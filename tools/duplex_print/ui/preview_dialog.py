"""手动双面打印的拼合预览：按纸张逐张显示正反两面，模拟打印装订后的成品。

为什么按"纸张"而不是按"文件"预览：用户真正会遇到的困惑是"打出来合在一起
页码乱了"，那是个配对问题——第 1 张纸的两面应该是第 1 页和第 2 页。所以这里
把两份打印文件按物理流程合回一叠纸（simulate_sheets），逐张并排显示。

缩略图显示的是**源页原样**，也就是翻面后实际看到的样子：三种翻面方式下工具
都已经把翻面造成的颠倒抵消掉了（上下翻转时反面文件里的页面自己转了 180°），
成品拿在手里读都是正的。文件层面的旋转用文字角标说明，免得用户以为打错了。

页数多时逐行懒加载：只有滚动到可见范围的行才去渲染，渲染结果按页号缓存。
"""

from pathlib import Path

import fitz
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from tools.duplex_print.core.reorderer import (
    FLIP_LONG_EDGE,
    FLIP_NONE,
    back_needs_rotation,
    simulate_sheets,
)

#: 缩略图显示宽度（px）。渲染分辨率取 72dpi（A4 约 595px 宽）再缩到这个宽度，
#: 缩放比约 1:4，既清晰又比按 200dpi 渲染快一个数量级。
THUMB_WIDTH = 168
RENDER_DPI = 72
#: 懒加载的预渲染余量：视口外这么远的行也提前渲染，滚动时不至于看到空位
LAZY_MARGIN = 400

DIALOG_STYLE = (
    "QDialog { background-color: #F9FAFB; }"
    "QScrollArea { background-color: #F9FAFB; border: none; }"
)

ROW_STYLE = (
    "QFrame#sheetRow {"
    "  background-color: #FFFFFF;"
    "  border: 1px solid #E5E7EB;"
    "  border-radius: 10px;"
    "}"
)

THUMB_BOX_STYLE = (
    "background-color: #FFFFFF;"
    " border: 1px solid #E5E7EB;"
    " border-radius: 4px;"
)

TITLE_STYLE = "font-size: 13px; font-weight: 700; color: #111827; background: transparent; border: none;"
CAPTION_STYLE = "font-size: 12px; font-weight: 600; color: #4F46E5; background: transparent; border: none;"
CAPTION_BLANK_STYLE = "font-size: 12px; font-weight: 600; color: #9CA3AF; background: transparent; border: none;"
NOTE_STYLE = "font-size: 11px; color: #B45309; background: transparent; border: none;"
SUMMARY_STYLE = "font-size: 12px; color: #374151; background: transparent; border: none;"
HINT_STYLE = "font-size: 11px; color: #6B7280; background: transparent; border: none;"

CLOSE_BTN = (
    "QPushButton {"
    "  background-color: #4F46E5;"
    "  color: #FFFFFF;"
    "  border: none;"
    "  border-radius: 8px;"
    "  padding: 8px 24px;"
    "  font-size: 13px;"
    "  font-weight: 600;"
    "  min-height: 34px;"
    "}"
    "QPushButton:hover { background-color: #4338CA; }"
)


class _PageRenderer:
    """共享一个源 PDF 句柄，把页面渲染成缩略图并按页号缓存。"""

    def __init__(self, src_path: str, thumb_width: int):
        self._doc = fitz.open(src_path)
        self._thumb_width = thumb_width
        self._cache: dict[int, QPixmap] = {}

    def close(self) -> None:
        self._doc.close()

    def aspect(self) -> float:
        """首页的高宽比，用于在渲染前就定下行高（懒加载要靠它算位置）。"""
        rect = self._doc[0].rect
        return (rect.height / rect.width) if rect.width else 1.414

    def pixmap(self, page_index: int) -> QPixmap:
        """第 page_index 页（0-based）的缩略图。"""
        cached = self._cache.get(page_index)
        if cached is None:
            pix = self._doc[page_index].get_pixmap(dpi=RENDER_DPI, alpha=False)
            # QImage 只是引用 pix.samples 的内存，必须立刻 copy 出来
            image = QImage(
                pix.samples, pix.width, pix.height, pix.stride,
                QImage.Format.Format_RGB888,
            ).copy()
            cached = QPixmap.fromImage(image)
            self._cache[page_index] = cached
        return cached


def _blank_pixmap(width: int, height: int) -> QPixmap:
    """空白占位页的示意图：虚线框 + 「空白面」。"""
    pixmap = QPixmap(width, height)
    pixmap.fill(QColor("#FFFFFF"))
    painter = QPainter(pixmap)
    painter.setPen(QPen(QColor("#D1D5DB"), 1, Qt.PenStyle.DashLine))
    painter.drawRect(0, 0, width - 1, height - 1)
    painter.setPen(QColor("#9CA3AF"))
    painter.setFont(QFont("Microsoft YaHei", 9))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "空白面")
    painter.end()
    return pixmap


class _ThumbColumn(QWidget):
    """一面缩略图 + 其下的说明文字。"""

    def __init__(self, width: int, height: int, caption: str, caption_style: str,
                 note: str = "", parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.image = QLabel()
        self.image.setFixedSize(width, height)
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setStyleSheet(THUMB_BOX_STYLE)
        layout.addWidget(self.image, alignment=Qt.AlignmentFlag.AlignHCenter)

        self.caption = QLabel(caption)
        self.caption.setStyleSheet(caption_style)
        self.caption.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.caption)

        if note:
            self.note = QLabel(note)
            self.note.setStyleSheet(NOTE_STYLE)
            self.note.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            self.note.setWordWrap(True)
            layout.addWidget(self.note)

    def set_pixmap(self, pixmap: QPixmap) -> None:
        """按占位框尺寸等比缩放后显示（缩略图按框大小渲染，不撑破卡片）。"""
        self.image.setPixmap(pixmap.scaled(
            self.image.width(), self.image.height(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))


class _SheetRow(QFrame):
    """一张纸：正面缩略图 ↔ 反面缩略图，配页码标注。"""

    def __init__(self, index: int, front_page: int, back_page: int | None,
                 rotate_back: bool, thumb_w: int, thumb_h: int, parent=None):
        super().__init__(parent)
        self.setObjectName("sheetRow")
        self.setStyleSheet(ROW_STYLE)
        self.is_rendered = False
        self._front_page = front_page
        self._back_page = back_page

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(8)

        title = QLabel(f"第 {index} 张纸")
        title.setStyleSheet(TITLE_STYLE)
        layout.addWidget(title)

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addStretch()

        self.front_col = _ThumbColumn(
            thumb_w, thumb_h, f"正面 · 第 {front_page} 页", CAPTION_STYLE
        )
        row.addWidget(self.front_col)

        arrow = QLabel("↔")
        arrow.setStyleSheet("font-size: 18px; color: #C7D2FE; background: transparent;")
        arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(arrow, alignment=Qt.AlignmentFlag.AlignVCenter)

        if back_page is None:
            # 空白面只会出现在最后一张纸（奇数页文档），占位图不依赖源文档
            self.back_col = _ThumbColumn(
                thumb_w, thumb_h, "反面 · 空白面", CAPTION_BLANK_STYLE,
                note="（奇数页文档的最后一张，属正常）",
            )
            self.back_col.image.setPixmap(_blank_pixmap(thumb_w, thumb_h))
        else:
            note = "（文件中已旋转 180°，翻面后正读）" if rotate_back else ""
            self.back_col = _ThumbColumn(
                thumb_w, thumb_h, f"反面 · 第 {back_page} 页", CAPTION_STYLE, note=note
            )
        row.addWidget(self.back_col)

        row.addStretch()
        layout.addLayout(row)

    def render_now(self, renderer: _PageRenderer) -> None:
        """渲染正反两面的缩略图（每行只做一次）。"""
        if self.is_rendered:
            return
        self.is_rendered = True
        self.front_col.set_pixmap(renderer.pixmap(self._front_page - 1))
        if self._back_page is not None:
            self.back_col.set_pixmap(renderer.pixmap(self._back_page - 1))


class DuplexPreviewDialog(QDialog):
    """打印后拼合效果的预览：逐张纸展示正反两面。"""

    def __init__(self, parent, src_path: str, page_count: int, flip: str):
        super().__init__(parent)
        self.setWindowTitle("拼合预览 — 打印后每张纸的正反面")
        self.setStyleSheet(DIALOG_STYLE)
        self.resize(780, 740)

        self._src_path = src_path
        self._page_count = page_count
        self._flip = flip
        self._rows: list[_SheetRow] = []
        self._renderer: _PageRenderer | None = None

        self._setup_ui()

    # ============================================================
    #  UI 构建
    # ============================================================

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(10)

        title = QLabel(f"📄 {Path(self._src_path).name}")
        title.setStyleSheet(TITLE_STYLE)
        root.addWidget(title)

        summary = QLabel(self._flip_summary())
        summary.setStyleSheet(SUMMARY_STYLE)
        summary.setWordWrap(True)
        root.addWidget(summary)

        hint = QLabel(
            "下面按纸张逐张列出成品：缩略图是翻面后实际看到的方向。"
            "核对每张纸的正反面页码是否连续，对了再打印。"
        )
        hint.setStyleSheet(HINT_STYLE)
        hint.setWordWrap(True)
        root.addWidget(hint)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        root.addWidget(self._scroll, stretch=1)

        self._content = QWidget()
        self._content.setStyleSheet("background: transparent;")
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(10)
        self._scroll.setWidget(self._content)

        self._build_rows()

        buttons = QHBoxLayout()
        buttons.addStretch()
        btn_close = QPushButton("关闭")
        btn_close.setStyleSheet(CLOSE_BTN)
        btn_close.clicked.connect(self.accept)
        buttons.addWidget(btn_close)
        root.addLayout(buttons)

        # 滚动到哪渲染到哪；初次显示也要渲染一次可见范围
        self._scroll.verticalScrollBar().valueChanged.connect(self._render_visible)

    def _build_rows(self):
        """建立所有纸张的行（先占位，缩略图等滚到可见范围再渲染）。"""
        try:
            self._renderer = _PageRenderer(self._src_path, THUMB_WIDTH)
        except Exception as exc:  # noqa: BLE001 - 打开失败就在面板里说明
            error = QLabel(f"无法读取源 PDF：{exc}")
            error.setStyleSheet(CAPTION_BLANK_STYLE)
            error.setWordWrap(True)
            self._content_layout.addWidget(error)
            self._content_layout.addStretch()
            return

        thumb_h = int(THUMB_WIDTH * self._renderer.aspect())
        rotate_back = back_needs_rotation(self._flip)
        for index, (front_page, back_page) in enumerate(
            simulate_sheets(self._page_count, self._flip), start=1
        ):
            row = _SheetRow(index, front_page, back_page, rotate_back,
                            THUMB_WIDTH, thumb_h)
            self._content_layout.addWidget(row)
            self._rows.append(row)
        self._content_layout.addStretch()

    def _flip_summary(self) -> str:
        if self._flip == FLIP_NONE:
            return (
                "翻面方式：不翻面（纸头不动）—— 整叠纸原样放回进纸盒，纸叠顺序不变，"
                "所以反面文件是按「从最后一张纸往前」的顺序排的。"
            )
        if self._flip == FLIP_LONG_EDGE:
            return (
                "翻面方式：左右反转（像翻书页，以长边为轴）—— "
                "整叠翻面后顺序反转，反面文件按正序打印；每页已预先旋转 180°，翻过来就是正的。"
            )
        return (
            "翻面方式：上下翻转（纸头调转，以短边为轴）—— "
            "整叠翻面后顺序反转，反面文件按正序打印，反面页不旋转。"
        )

    # ============================================================
    #  懒加载
    # ============================================================

    def showEvent(self, event):
        super().showEvent(event)
        # 布局还没算完时行高为 0，等一轮事件循环再渲染首屏
        self._render_visible()

    def _render_visible(self):
        if self._renderer is None:
            return
        top = self._scroll.verticalScrollBar().value()
        bottom = top + self._scroll.viewport().height()
        for row in self._rows:
            if row.is_rendered:
                continue
            y = row.y()
            if y + row.height() >= top - LAZY_MARGIN and y <= bottom + LAZY_MARGIN:
                row.render_now(self._renderer)

    # ============================================================
    #  资源释放
    # ============================================================

    def closeEvent(self, event):
        self._release_renderer()
        super().closeEvent(event)

    def done(self, result: int):
        self._release_renderer()
        super().done(result)

    def _release_renderer(self):
        if self._renderer is not None:
            self._renderer.close()
            self._renderer = None

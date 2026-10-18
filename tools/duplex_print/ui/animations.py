"""手动双面打印的动画演示组件。

用 QPainter + QTimer 逐帧绘制，循环演示两种打印机行为：
    - PaperExitDemo：纸张从打印机口滑出落到纸盘，展示出纸面朝上/朝下时
      用户看到的是"印好的一面"还是"纸的背面"。
    - PaperFlipDemo：演示三种翻面方式（不翻面 / 左右反转 / 上下翻转），
      展示纸叠顺序变不变、反面文字与正面是同向还是相反。

绘制逻辑独立为 paint_frame(painter, w, h, t)，便于离屏渲染自检。
"""

import math

from PyQt6.QtCore import QElapsedTimer, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import QWidget

from tools.duplex_print.core.reorderer import (
    FLIP_LONG_EDGE,
    FLIP_NONE,
    FLIP_SHORT_EDGE,
    back_needs_rotation,
)

ANIM_FPS = 30

DEMO_BG_STYLE = (
    "background-color: #F9FAFB;"
    " border: 1px solid #E5E7EB;"
    " border-radius: 8px;"
)

# 配色（Slate + Indigo 设计系统）
C_PRIMARY = QColor("#4F46E5")
C_INK = QColor("#374151")
C_MUTED = QColor("#9CA3AF")
C_BORDER = QColor("#D1D5DB")
C_PAPER = QColor("#FFFFFF")
C_GREEN = QColor("#059669")
C_AMBER = QColor("#B45309")
C_TEXT_LINE = QColor("#C7D2FE")
# 纸头标记：进纸时先被打印机咬住的那条边，也是内容正上方的方向
C_HEAD = QColor("#F59E0B")


def _draw_head_marker(p: QPainter, x: float, y: float, w: float):
    """在纸张靠"纸头"那条边画一条橙线。

    纸头＝打印机进纸时先咬住的那条边。翻面动画整体做了镜像变换，这条线会
    自动跟着跑到对面去，用户对着它就能确认自己的翻面动作属于哪一种——比
    "左右翻还是上下翻"可靠得多，同一句话两个人可能指的是两条不同的轴。
    """
    p.save()
    p.setPen(QPen(C_HEAD, 3))
    p.drawLine(int(x + 4), int(y + 2), int(x + w - 4), int(y + 2))
    p.restore()


def _draw_sheet(p: QPainter, x: float, y: float, w: float, h: float, with_content: bool,
                dimmed: bool = False):
    """画一张纸。with_content=True 时画"印好的一面"（模拟文字行）。

    dimmed 用于纸叠里被压在下面的那些纸：浅灰底 + 浅边框，让最上面那张
    一眼可辨——它翻成空白面朝上时才不会和下面几张糊成一片白。
    """
    p.save()
    p.setPen(QPen(C_BORDER, 1))
    p.setBrush(QColor("#F3F4F6") if dimmed else C_PAPER)
    p.drawRect(int(x), int(y), int(w), int(h))
    if with_content:
        p.setPen(QPen(C_TEXT_LINE, 1.6))
        p.drawLine(int(x + 5), int(y + 8), int(x + w - 5), int(y + 8))
        p.drawLine(int(x + 5), int(y + 13), int(x + w - 9), int(y + 13))
        p.drawLine(int(x + 5), int(y + 18), int(x + w - 6), int(y + 18))
    p.restore()


def _label_font() -> QFont:
    font = QFont("Microsoft YaHei", 8)
    return font


class PaperExitDemo(QWidget):
    """演示打印机出纸：印好的一面朝上还是朝下。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._face_down = True
        self.setMinimumHeight(104)
        self.setStyleSheet(DEMO_BG_STYLE)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update)
        self._clock = QElapsedTimer()
        self._clock.start()

    def set_face_down(self, face_down: bool):
        """切换演示模式；模式变化时重置动画。"""
        if self._face_down != face_down:
            self._face_down = face_down
            self._clock.restart()
            self.update()

    def showEvent(self, event):
        super().showEvent(event)
        self._clock.restart()
        self._timer.start(1000 // ANIM_FPS)

    def hideEvent(self, event):
        super().hideEvent(event)
        self._timer.stop()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.paint_frame(p, self.width(), self.height(), self._clock.elapsed() / 1000.0)
        p.end()

    def paint_frame(self, p: QPainter, w: int, h: int, t: float):
        """绘制指定时刻 t（秒）的画面。

        场景按 340px 宽设计，实际控件更宽时整体水平居中。
        """
        p.save()
        p.translate(max(0, (w - 340) / 2), 0)
        # ---- 打印机机身（左侧）----
        p.setPen(QPen(C_MUTED, 1))
        p.setBrush(QColor("#F3F4F6"))
        p.drawRoundedRect(10, 10, 64, 44, 8, 8)
        # 出纸口（机身右侧的深色缝隙）
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#6B7280"))
        p.drawRect(74, 18, 5, 28)
        p.setPen(C_MUTED)
        p.setFont(_label_font())
        p.drawText(10, 66, "打印机")

        # ---- 出纸托盘（右下）----
        p.setPen(QPen(C_BORDER, 1))
        p.drawLine(10, h - 14, w - 12, h - 14)

        # ---- 纸叠：底纸上永远有一张（朝向随模式）----
        sheet_w, sheet_h = 40, 24
        stack_x = 150
        bottom_y = h - 14 - sheet_h
        top_y = bottom_y - sheet_h + 2
        _draw_sheet(p, stack_x, bottom_y, sheet_w, sheet_h, not self._face_down)

        # ---- 动态纸：从出纸口滑出 → 落到纸叠顶 → 循环 ----
        cycle = 1.8
        u = (t % cycle) / cycle
        if u < 0.45:  # 水平滑出
            x = 80 + (stack_x - 80) * (u / 0.45)
            y = 26
        elif u < 0.62:  # 落到纸叠顶
            x = stack_x
            y = 26 + (top_y - 26) * ((u - 0.45) / 0.17)
        else:  # 停在纸叠顶
            x = stack_x
            y = top_y
        _draw_sheet(p, x, y, sheet_w, sheet_h, not self._face_down)

        # ---- 标注：观众看到的这一面 ----
        p.setPen(C_INK)
        p.setFont(_label_font())
        label_x = stack_x + sheet_w + 10
        if self._face_down:
            p.drawText(label_x, 34, "你看到的是纸的背面")
            p.setPen(C_MUTED)
            p.drawText(label_x, 48, "（空白面朝上）")
        else:
            p.drawText(label_x, 34, "你看到的是印好的一面")
            p.setPen(C_MUTED)
            p.drawText(label_x, 48, "（文字面朝上）")
        p.restore()


class PaperFlipDemo(QWidget):
    """演示一叠纸的三种翻面流程：不翻面 / 左右反转（书式）/ 上下翻转（纸头调转）。

    与页序的对应关系（这正是"反面文件页序为什么这么排"的直观解释）：
        - FLIP_NONE：整叠直接放回进纸盒，纸叠顺序不变——反面文件要从最后一页
          往前印；奇数页文档还要在最前面补一张空白页占位。
        - FLIP_LONG_EDGE：拿起纸叠左右反转（像翻书页，以长边为轴）——纸叠顺序
          反转，反面文件正序打印即可；反面页要预转 180°（见 back_needs_rotation）。
        - FLIP_SHORT_EDGE：上下翻转（纸头调转，以短边为轴）——纸叠顺序同样反转，
          反面文件正序打印，反面页不旋转。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._flip = FLIP_LONG_EDGE
        self.setMinimumHeight(110)
        self.setStyleSheet(DEMO_BG_STYLE)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update)
        self._clock = QElapsedTimer()
        self._clock.start()

    def set_flip(self, flip: str):
        """切换演示模式；模式变化时重置动画。"""
        if self._flip != flip:
            self._flip = flip
            self._clock.restart()
            self.update()

    def showEvent(self, event):
        super().showEvent(event)
        self._clock.restart()
        self._timer.start(1000 // ANIM_FPS)

    def hideEvent(self, event):
        super().hideEvent(event)
        self._timer.stop()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.paint_frame(p, self.width(), self.height(), self._clock.elapsed() / 1000.0)
        p.end()

    def paint_frame(self, p: QPainter, w: int, h: int, t: float):
        """绘制指定时刻 t（秒）的画面。

        翻转用 2D 投影模拟 3D：
            - 左右反转（书式，绕前后轴）→ 水平方向缩放 |cosθ|；
            - 上下翻转（纸头调转，绕左右轴）→ 垂直方向缩放 |cosθ|；
        θ 过 π/2 时内容镜像（此时纸叠顺序也反转）。
        """
        sheet_w, sheet_h, off = 52, 68, 3
        # 纸叠略偏左，右侧留给"现在朝上的是哪一面"的说明
        cx = w / 2 - 42
        cy = (h - 14) / 2
        # 纸叠包围盒左上角（以中心对齐）
        bx = cx - (sheet_w + off) / 2
        by = cy - (sheet_h + off) / 2

        p.setFont(_label_font())
        if self._flip == FLIP_NONE:
            # 不翻面：整叠被拿起又放回，顺序与朝向都不变
            lift = 5 * (1 - math.cos(2 * math.pi * t / 2.6)) / 2
            p.save()
            p.translate(0, -lift)
            self._draw_stack(p, bx, by, sheet_w, sheet_h, off, mirrored=False, blank_up=False)
            p.restore()
            self._draw_face_label(p, w, h, self._flip, False)
            p.setPen(C_INK)
            p.drawText(0, h - 4, "不翻面：整叠直接放回进纸盒 → 纸叠顺序不变，反面文件从最后一页往前印")
            return

        # ---- 翻转相位：θ 在 0→π→0 间平滑往返，两端自然停顿 ----
        theta = math.pi * (1 - math.cos(2 * math.pi * t / 2.6)) / 2
        cos_t = math.cos(theta)
        mirrored = cos_t < 0
        # 防止缩放为 0 时笔画消失
        scale = max(abs(cos_t), 0.02)

        p.save()
        p.translate(cx, cy)
        if self._flip == FLIP_SHORT_EDGE:
            p.scale(1, scale)   # 上下翻转（纸头调转）：垂直压缩
        else:
            p.scale(scale, 1)   # 左右反转（书式）：水平压缩
        if mirrored:
            if self._flip == FLIP_SHORT_EDGE:
                p.scale(1, -1)  # 纸头调转：垂直镜像
            else:
                p.scale(-1, 1)  # 书式翻：水平镜像
        p.translate(-cx, -cy)
        # 翻面过半、顺序反转之后，翻上来的那一张朝上的正是**空白面**——
        # 这就是翻面的目的，动画必须让人看到它，否则会被误读成"翻过来还是字面"。
        self._draw_stack(p, bx, by, sheet_w, sheet_h, off,
                         mirrored=mirrored, blank_up=mirrored)
        p.restore()

        self._draw_face_label(p, w, h, self._flip, mirrored)

        # ---- 底部标注：翻转结果 ----
        if self._flip == FLIP_SHORT_EDGE:
            p.setPen(C_GREEN)
            p.drawText(0, h - 4, "上下翻转（纸头调转，短边为轴）→ 反面页不旋转，与正面同向")
        else:
            p.setPen(C_AMBER)
            p.drawText(0, h - 4, "左右反转（像翻书页，长边为轴）→ 工具自动旋转反面页 180°，翻面后正读")

    @staticmethod
    def _draw_stack(p: QPainter, bx: float, by: float, sheet_w: float, sheet_h: float,
                    off: float, mirrored: bool, blank_up: bool):
        """画一叠 3 张纸。

        mirrored 表示已经翻过一半（纸叠顺序随之反转）；
        blank_up 表示最上面那张纸朝天的那一面是空白面，
        此时最上面只剩一个加粗边框的白框——一眼看出"翻上来的是背面"。
        """
        orders = [(0, 0), (off, off), (off * 2, off * 2)]
        if mirrored:
            orders = list(reversed(orders))
        # 从最底下那张开始画，最后画最上面那张
        for dx, dy in reversed(orders):
            _draw_sheet(p, bx + dx, by + dy, sheet_w, sheet_h,
                        with_content=False, dimmed=True)

        top_dx, top_dy = orders[0]
        top_x, top_y = bx + top_dx, by + top_dy
        _draw_sheet(p, top_x, top_y, sheet_w, sheet_h, with_content=not blank_up)
        if not blank_up:
            # 印好的一面：蓝色标题条 + 红色箭头，方向一目了然
            p.setPen(QPen(C_PRIMARY, 2.6))
            p.drawLine(int(top_x + 6), int(top_y + 14),
                       int(top_x + sheet_w - 6), int(top_y + 14))
            p.setPen(QPen(QColor("#DC2626"), 1.8))
            p.drawLine(int(top_x + 16), int(top_y + 42), int(top_x + 38), int(top_y + 58))
            p.drawLine(int(top_x + 38), int(top_y + 58), int(top_x + 30), int(top_y + 57))
            p.drawLine(int(top_x + 38), int(top_y + 58), int(top_x + 37), int(top_y + 50))
        # 纸头标记画在纸的固定那条边上，镜像变换会带着它跑到对面去
        _draw_head_marker(p, top_x, top_y, sheet_w)

    @staticmethod
    def _draw_face_label(p: QPainter, w: int, h: int, flip: str, mirrored: bool):
        """纸叠右侧的说明：现在朝上的是哪一面、反面要不要预转。

        要不要预转因机器而异，统一由 back_needs_rotation 给出，动画只负责
        把它和"翻面后是哪一面朝上"一起讲清楚。
        """
        x = int(w / 2 + 18)
        y = int(h / 2 - 22)
        p.setPen(C_MUTED)
        p.drawText(x, y, "橙线＝纸头")

        if flip == FLIP_NONE:
            p.setPen(C_INK)
            p.drawText(x, y + 17, "没翻面")
            p.setPen(C_MUTED)
            p.drawText(x, y + 34, "→ 纸头不动，反面不转")
            return

        # 翻面过半后朝上的都是空白面；要不要预转则取决于翻的是哪条轴
        # （这是因机型而异的实测值，统一由 core/reorderer.py 的
        #  back_needs_rotation 给出，界面各处别再各判一遍）
        p.setPen(C_GREEN if mirrored else C_INK)
        p.drawText(x, y + 17, "空白面朝上" if mirrored else "印好的一面朝上")
        if back_needs_rotation(flip):
            p.setPen(C_AMBER)
            p.drawText(x, y + 34, "→ 反面预转 180°")
        else:
            p.setPen(C_MUTED)
            p.drawText(x, y + 34, "→ 反面不转，与正面同向")

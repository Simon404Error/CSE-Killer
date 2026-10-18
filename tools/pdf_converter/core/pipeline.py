"""转换流水线编排器: 串联 解析→清洗→排版→生成 全流程。"""

from typing import Callable

from tools.pdf_converter.core.models import ParsedDocument, CleanedDocument, LaidOutDocument
from tools.pdf_converter.core.parser.text_parser import TextParser
from tools.pdf_converter.core.cleaner.xingce_cleaner import XingceCleaner, detect_exam_type
from tools.pdf_converter.core.cleaner.shenlun_cleaner import ShenlunCleaner
from tools.pdf_converter.core.layout.engine import LayoutEngine, LayoutConfig
from tools.pdf_converter.core.generator.font_manager import FontManager
from tools.pdf_converter.core.generator.pdf_generator import PDFGenerator
from tools.pdf_converter.config.settings import load_template


SCANNED_PDF_MESSAGE = (
    "该PDF为扫描件（图片型），暂不支持转换。\n"
    "请使用刷题App导出的文字版PDF。"
)


class ScannedPdfError(ValueError):
    """扫描件/图片型 PDF 无法转换为可重排的真题格式。"""

    def __init__(self, message: str = SCANNED_PDF_MESSAGE):
        super().__init__(message)


class ConversionPipeline:
    """PDF格式转换流水线。

    用法:
        pipeline = ConversionPipeline()
        output_pdf_bytes = pipeline.run("input.pdf", progress_callback)
    """

    def __init__(self):
        self._font_manager = FontManager()
        self._font_manager.register_all()

    def run(self, input_path: str,
            progress: Callable[[int, str], None] = None,
            template_name: str = "xingce",
            config_overrides: dict | None = None) -> bytes:
        """执行完整的转换流水线。

        Args:
            input_path: 输入PDF路径
            progress: 进度回调 (percent, stage_name)
            template_name: 模板名称 ("xingce" 或 "shenlun")
            config_overrides: UI设置覆盖，如 {'body_font':'FangSong', 'body_size':14.0, 'page_size':'B5', ...}

        Returns:
            输出PDF的二进制数据
        """
        overrides = config_overrides or {}

        if progress:
            progress(0, "解析PDF...")
        parsed = self._parse(input_path, progress)

        if progress:
            progress(20, "检测题型...")
        exam_type = template_name if template_name in ("xingce", "shenlun") \
            else detect_exam_type(parsed)

        if progress:
            progress(30, "清洗内容...")
        cleaned = self._clean(parsed, exam_type, progress)

        if not cleaned.questions and not cleaned.shenlun_questions and not cleaned.materials:
            raise ValueError(
                "未能从PDF中提取到任何题目。\n"
                "可能原因：\n"
                "1. PDF内容不是公务员考试题目格式\n"
                "2. PDF文字层缺失或排版过于复杂\n"
                "请检查PDF内容是否为刷题App导出的文字版试卷。"
            )

        if progress:
            progress(50, "计算排版...")
        # 收集所有图片
        ignored_pages = set(cleaned.ignored_pages)
        all_images = []
        for pg in parsed.pages:
            if pg.page_number in ignored_pages:
                continue
            all_images.extend(pg.images)
        laid_out = self._layout(cleaned, exam_type, progress, overrides, all_images)

        if progress:
            progress(70, "生成PDF...")
        output = self._generate(laid_out, progress)

        if overrides.get("keep_last_page"):
            output = self._append_last_page(input_path, output, exam_type, overrides)

        if progress:
            progress(100, "完成!")

        return output

    def _parse(self, path: str,
               progress: Callable[[int, str], None] = None) -> ParsedDocument:
        """解析PDF文件 — 仅支持文字型 PDF，扫描件直接拒绝。"""
        if TextParser.detect_pdf_type(path) == "image":
            raise ScannedPdfError()

        return TextParser().parse(path, progress)

    def _clean(self, doc: ParsedDocument, exam_type: str,
               progress: Callable[[int, str], None] = None) -> CleanedDocument:
        """清洗文档内容。"""
        if exam_type == "shenlun":
            cleaner = ShenlunCleaner()
        else:
            cleaner = XingceCleaner()
        return cleaner.clean(doc, progress)

    def _layout(self, doc: CleanedDocument, exam_type: str,
                progress: Callable[[int, str], None] = None,
                overrides: dict | None = None,
                images: list | None = None) -> LaidOutDocument:
        """排版文档。"""
        template = load_template(exam_type)
        config = LayoutConfig.from_template(template, overrides or {})

        engine = LayoutEngine(config)
        result = engine.layout(doc, images=images)
        engine.finish()

        if progress:
            progress(65, f"排版完成: {result.total_pages} 页")

        return result

    def _generate(self, doc: LaidOutDocument,
                  progress: Callable[[int, str], None] = None) -> bytes:
        """生成最终PDF。"""
        generator = PDFGenerator(self._font_manager)
        output = generator.generate(doc)

        if progress:
            progress(95, "输出完成")

        return output

    @staticmethod
    def _append_last_page(input_path: str, output: bytes, exam_type: str,
                          overrides: dict | None = None) -> bytes:
        """将源 PDF 最后一页以图片形式追加到输出 PDF 末尾。

        用于"保留尾页（对答案二维码）"功能：源 PDF 最后一页通常包含
        二维码和广告文字，以原样截图方式保留，不做文字重排。
        """
        import io
        import fitz
        from reportlab.lib.units import mm
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfgen import canvas

        template = load_template(exam_type)
        config = LayoutConfig.from_template(template, overrides or {})
        page_w_mm = config.page_width_mm
        page_h_mm = config.page_height_mm
        content_w_mm = max(20.0, page_w_mm - config.margin_left - config.margin_right)
        content_h_mm = max(20.0, page_h_mm - config.margin_top - config.margin_bottom)

        with fitz.open(input_path) as src:
            if src.page_count < 1:
                return output
            last_page = src[-1]
            pix = last_page.get_pixmap(dpi=200, alpha=False)
            img_bytes = pix.tobytes("png")
            src_w_mm = last_page.rect.width * 25.4 / 72
            src_h_mm = last_page.rect.height * 25.4 / 72

        scale = min(
            content_w_mm / max(src_w_mm, 1.0),
            content_h_mm / max(src_h_mm, 1.0),
        )
        draw_w_mm = src_w_mm * scale
        draw_h_mm = src_h_mm * scale
        x_mm = config.margin_left + (content_w_mm - draw_w_mm) / 2
        y_top_mm = config.margin_top + (content_h_mm - draw_h_mm) / 2

        # 用 PyMuPDF 将最后一页追加到已有输出 PDF
        out_doc = fitz.open(stream=output, filetype="pdf")
        # 创建单页 PDF 包含源最后一页的截图
        tail_buf = io.BytesIO()
        c = canvas.Canvas(tail_buf, pagesize=(page_w_mm * mm, page_h_mm * mm))
        reader = ImageReader(io.BytesIO(img_bytes))
        c.drawImage(
            reader,
            x_mm * mm,
            (page_h_mm - y_top_mm - draw_h_mm) * mm,
            width=draw_w_mm * mm,
            height=draw_h_mm * mm,
            preserveAspectRatio=True,
        )
        c.save()
        tail_bytes = tail_buf.getvalue()

        # 将截图页插入到输出文档末尾
        tail_doc = fitz.open(stream=tail_bytes, filetype="pdf")
        out_doc.insert_pdf(tail_doc)
        result = out_doc.tobytes()
        out_doc.close()
        tail_doc.close()
        return result

    def get_available_fonts(self) -> list[str]:
        """获取可用字体列表(供UI使用)。"""
        return self._font_manager.available_fonts()

    def get_template_names(self) -> list[str]:
        """获取可用模板列表。"""
        return ["xingce", "shenlun"]

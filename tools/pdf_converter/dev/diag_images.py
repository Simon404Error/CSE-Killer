"""诊断脚本: 输出源 PDF 每页图片池与清洗后题目的源区间, 定位图片丢失/错配。

用法:
    python tools/pdf_converter/dev/diag_images.py <input.pdf> <输出txt>
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)

from tools.pdf_converter.config.settings import load_template
from tools.pdf_converter.core.cleaner.xingce_cleaner import XingceCleaner
from tools.pdf_converter.core.generator.font_manager import FontManager
from tools.pdf_converter.core.layout.engine import LayoutEngine, LayoutConfig
from tools.pdf_converter.core.parser.text_parser import TextParser


def main() -> None:
    input_path = sys.argv[1]
    out_path = sys.argv[2]

    FontManager().register_all()
    parsed = TextParser().parse(input_path)
    cleaned = XingceCleaner().clean(parsed)

    lines: list[str] = []
    w = lines.append

    w(f"# 源文件: {os.path.basename(input_path)}")
    w(f"# 解析页数: {len(parsed.pages)}  题目数: {len(cleaned.questions)}  "
      f"ignored_pages={cleaned.ignored_pages}")
    w("")

    w("## 每页图片池")
    for pg in parsed.pages:
        w(f"  第{pg.page_number}页: {len(pg.images)} 张")
        for img in pg.images:
            w(f"      x={img.bbox[0]:7.2f} y={img.bbox[1]:7.2f} "
              f"w={img.width_mm:6.2f} h={img.height_mm:6.2f}  bytes={len(img.image_bytes)}")
    w("")

    w("## 清洗后题目源区间")
    for q in cleaned.questions:
        w(f"  第{q.number}题  src_p={q.source_page} y={getattr(q,'source_y_mm',None)} "
          f"end_p={getattr(q,'source_end_page',None)} end_y={getattr(q,'source_end_y_mm',None)} "
          f"last_p={getattr(q,'source_last_page',None)} last_y={getattr(q,'source_last_y_mm',None)} "
          f"prev_p={getattr(q,'source_prev_last_page',None)} prev_y={getattr(q,'source_prev_last_y_mm',None)} "
          f"is_da={getattr(q,'is_data_analysis',False)} opts={len(q.options)}")
        w(f"      题干: {(q.stem or '')[:110]}")
        for opt in q.options:
            w(f"      {opt.label}. {(opt.text or '')[:70]}")
    w("")

    # 排版阶段: 记录图片去向
    config = LayoutConfig.from_template(load_template("xingce"), {})
    ignored = set(cleaned.ignored_pages)
    images = [img for pg in parsed.pages if pg.page_number not in ignored for img in pg.images]
    engine = LayoutEngine(config)
    laid = engine.layout(cleaned, images=images)
    engine.finish()

    w("## 排版后图片元素 (按版面顺序)")
    for page in laid.pages:
        last_label = "?"
        for el in sorted(page.elements, key=lambda e: (round(e.y_mm, 1), e.x_mm)):
            if el.type == "question_number":
                last_label = (el.text or "").strip()
            elif el.type == "image":
                w(f"  输出p{page.page_number} y={el.y_mm:7.2f} {el.image_w_mm:6.2f}x{el.image_h_mm:6.2f}mm "
                  f"  前题={last_label}")
    w("")

    w("## 未被任何题目消费的剩余图片 (排版后残留)")
    leftover = engine._images_by_page if hasattr(engine, "_images_by_page") else {}
    for page_no in sorted(leftover):
        for img in leftover[page_no]:
            w(f"  源p{page_no} x={img.bbox[0]:7.2f} y={img.bbox[1]:7.2f} "
              f"w={img.width_mm:6.2f} h={img.height_mm:6.2f}")

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print(f"written {out_path}")


if __name__ == "__main__":
    main()

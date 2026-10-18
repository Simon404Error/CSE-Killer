"""回归脚本: 锁定三个已修复缺陷, 防止后续改动再次退化。

  问题1 资料分析"专项智能练习（资料分析）.pdf"
        - 第4题 四个选项饼图(19.6×19.2mm, 曾被"边长<20mm"规则误杀)必须出现
        - 第20题 四个选项饼图(排在题干所在页的下一页, 曾整体漏取)必须出现
  问题2 图形推理"专项智能练习（图形推理）.pdf"
        - 第5页只应解析出紧贴题图的一张图; 曾把整页白色背景矩形与题图
          合并成"原 PDF 截图", 输出里出现页眉/水印/题号一起被拍下的画面
  问题3 批量转换"保留尾页"
        - 主界面排版设置勾选后, 批量对话框必须继承; 每个 PDF 各自保留自己的尾页

用法:
    python tools/pdf_converter/dev/regression_3bugs.py [样本目录]
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)

import fitz  # noqa: E402

from tools.pdf_converter.config.settings import load_template  # noqa: E402
from tools.pdf_converter.core.cleaner.xingce_cleaner import XingceCleaner  # noqa: E402
from tools.pdf_converter.core.generator.font_manager import FontManager  # noqa: E402
from tools.pdf_converter.core.layout.engine import LayoutEngine, LayoutConfig  # noqa: E402
from tools.pdf_converter.core.parser.text_parser import TextParser  # noqa: E402
from tools.pdf_converter.core.pipeline import ConversionPipeline  # noqa: E402

ZILIAO = "专项智能练习（资料分析）.pdf"
TUXING = "专项智能练习（图形推理）.pdf"

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def output_images(pdf_bytes: bytes) -> list[tuple[float, float, float]]:
    """列出输出 PDF 中的图片: [(宽mm, 高mm, 所在页), ...]"""
    rows: list[tuple[float, float, float]] = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page in doc:
            seen = set()
            for info in page.get_images(full=True):
                xref = info[0]
                if xref in seen:
                    continue
                seen.add(xref)
                for rect in page.get_image_rects(xref):
                    rows.append((rect.width * 25.4 / 72, rect.height * 25.4 / 72, page.number + 1))
    return rows


def count_in_range(images, lo: float, hi: float) -> int:
    return sum(1 for w, _h, _p in images if lo <= w <= hi)


def test_ziliao(sample_dir: str, pipeline: ConversionPipeline) -> None:
    print(f"\n问题1 · {ZILIAO}")
    path = os.path.join(sample_dir, ZILIAO)
    parsed = TextParser().parse(path)
    cleaned = XingceCleaner().clean(parsed)

    q4 = next((q for q in cleaned.questions if q.number == 4), None)
    q20 = next((q for q in cleaned.questions if q.number == 20), None)
    check("解析出第4题与第20题", q4 is not None and q20 is not None)
    if q4 is None or q20 is None:
        return

    config = LayoutConfig.from_template(load_template("xingce"), {})
    images = [img for pg in parsed.pages for img in pg.images]
    engine = LayoutEngine(config)
    engine.layout(cleaned, images=images)

    out = pipeline.run(path, template_name="xingce", config_overrides={})
    rendered = output_images(out)

    # 第4题的饼图约 15.7mm 宽（源图 19.6mm × 0.8 缩放）
    q4_charts = count_in_range(rendered, 10.0, 20.0)
    check("第4题四张选项饼图都渲染出来", q4_charts >= 4, f"检出 {q4_charts} 张 10~20mm 图")

    # 第20题的饼图约 38mm 宽（源图 47.9mm × 0.8 缩放）
    q20_charts = count_in_range(rendered, 30.0, 45.0)
    check("第20题四张选项饼图都渲染出来", q20_charts >= 4, f"检出 {q20_charts} 张 30~45mm 图")


def test_tuxing(sample_dir: str, pipeline: ConversionPipeline) -> None:
    print(f"\n问题2 · {TUXING}")
    path = os.path.join(sample_dir, TUXING)
    parsed = TextParser().parse(path)

    page5 = next((p for p in parsed.pages if p.page_number == 5), None)
    check("解析出第5页", page5 is not None)
    if page5 is None:
        return

    widths = [img.width_mm for img in page5.images]
    check("第5页只解析出紧贴题图的一张图",
          len(page5.images) == 1, f"实际 {len(page5.images)} 张, 宽度 {[round(w, 1) for w in widths]}")
    check("该图不是整页截图",
          all(w < 120 for w in widths), f"最大宽度 {round(max(widths), 1) if widths else 0}mm")

    out = pipeline.run(path, template_name="xingce", config_overrides={})
    rendered = output_images(out)
    with fitz.open(stream=out, filetype="pdf") as doc:
        page_w = doc[0].rect.width * 25.4 / 72
    huge = [w for w, _h, _p in rendered if w > page_w * 0.9]
    check("输出中没有整幅宽的截图", not huge, f"越界图片宽度 {[round(w, 1) for w in huge]}")
    check("第10题的题图已渲染", count_in_range(rendered, 90.0, 110.0) >= 1)


def test_batch_keep_last_page(sample_dir: str) -> None:
    print("\n问题3 · 批量转换保留尾页")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PyQt6.QtWidgets import QApplication
    except Exception as exc:  # pragma: no cover
        check("PyQt6 可用", False, str(exc))
        return

    from tools.pdf_converter.ui.batch_dialog import BatchDialog
    from tools.pdf_converter.ui.settings_panel import SettingsPanel

    app = QApplication.instance() or QApplication([])
    fonts = ["SimSun", "FangSong", "SimHei", "KaiTi"]

    # 主界面排版设置里勾选"保留尾页"
    main_panel = SettingsPanel(fonts)
    main_panel.chk_keep_last_page.setChecked(True)
    main_config = main_panel.get_config()

    dialog = BatchDialog(fonts, None, main_config)
    check("批量对话框继承主界面的\"保留尾页\"",
          dialog.settings_panel.get_config()["keep_last_page"] is True)
    app.processEvents()

    files = [os.path.join(sample_dir, ZILIAO), os.path.join(sample_dir, TUXING)]
    dialog._add_paths(files)
    dialog._on_start()
    dialog._worker.wait(600_000)
    app.processEvents()

    check("两个文件都转换成功", len(dialog._results) == 2,
          f"成功 {len(dialog._results)}, 失败 {len(dialog._failed)}")

    # 正文重排后的页数会与源文件不同, 所以拿"同一份配置但不保留尾页"的结果作基准
    config = dialog.settings_panel.get_config()
    body_pipeline = ConversionPipeline()

    tails: dict[str, bytes] = {}
    for path in files:
        name = os.path.basename(path)
        pdf = dialog._results.get(name)
        if pdf is None:
            continue
        body = dict(config, keep_last_page=False)
        body_pages = fitz.open(
            stream=body_pipeline.run(path, template_name=config.get("template", "xingce"),
                                     config_overrides=body),
            filetype="pdf",
        ).page_count
        with fitz.open(stream=pdf, filetype="pdf") as doc:
            out_pages = doc.page_count
            tail_pix = doc[-1].get_pixmap(dpi=50, alpha=False).samples
        tails[name] = tail_pix
        check(f"{name} 尾页已保留", out_pages == body_pages + 1,
              f"正文 {body_pages} 页 -> 输出 {out_pages} 页（+1 尾页）")

    if len(tails) == 2:
        a, b = tails.values()
        check("每个 PDF 保留的是各自的尾页", a != b)


def main() -> None:
    sample_dir = sys.argv[1] if len(sys.argv) > 1 else ROOT
    FontManager().register_all()
    pipeline = ConversionPipeline()

    test_ziliao(sample_dir, pipeline)
    test_tuxing(sample_dir, pipeline)
    test_batch_keep_last_page(sample_dir)

    print("\n" + "=" * 60)
    if failures:
        print(f"失败 {len(failures)} 项:")
        for item in failures:
            print(f"  - {item}")
        raise SystemExit(1)
    print("全部通过")


if __name__ == "__main__":
    main()

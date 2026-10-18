"""回归脚本: 锁定手动双面打印重排的已修复缺陷。

背景（用户实测反馈）：
    反面文件里的表格打印出来是反的、整页位图的页面只打出几个被拉伸的字，
    但同一个 PDF 在屏幕上、在打印预览里都正常；同一批位图在正面文件里
    （原样拷贝、矩阵是正的）打印完全正常。

    原因是反面页的 180° 旋转无论用 /Rotate 标志位还是用 Form XObject 的
    /Matrix 表达，都会让位图的有效变换矩阵变成负的，部分打印机/驱动在这
    种情况下会把位图贴错。所以反面页的旋转改成：

      1. 旋转写进页面内容流最外层（q ... cm ... Q），/Rotate 保持 0；
      2. 位图像素自己转 180°，绘制处再套 [-1 0 0 -1 1 1] 抵消，
         使位图的有效矩阵回到"正的纯缩放平移"。

用法:
    QT_QPA_PLATFORM=offscreen python tools/duplex_print/dev/regression_duplex.py [样本PDF]
"""

import io
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)

import fitz  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageChops, ImageFilter  # noqa: E402

from tools.duplex_print.core import reorderer as reorderer_module  # noqa: E402
from tools.duplex_print.core.reorderer import (  # noqa: E402
    FLIP_LONG_EDGE,
    FLIP_NONE,
    FLIP_SHORT_EDGE,
    DuplexReorderer,
    _key,
    _resource_map,
    back_needs_rotation,
    page_orders,
)

# 首选的样本：桌面上那份"合并.pdf"（用户实际用的源文件，含大量位图页），
# 退回到仓库里的生成结果文件，保证脚本换个环境也能跑。
SAMPLE_CANDIDATES = [
    os.path.expanduser(r"~\Desktop\合并.pdf"),
    os.path.join(ROOT, "合并打印-正面.pdf"),
]

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}", flush=True)
    if not ok:
        failures.append(label)


def back_order(page_count: int, flip: str) -> list[int | None]:
    """反面文件的源页索引（0-based）；None 为空白占位页。"""
    return page_orders(page_count, flip)[1]


def test_paper_flow() -> None:
    """页序 · 纸张流转模拟：三种翻面动作下每张纸的正反面都必须配对。

    只依据三条与机型无关的事实推导，不看 reorderer 的实现：
      1. 进纸盒从纸叠最上面逐张取纸；
      2. 出纸后最后打印的那张压在纸叠最上面——这与出纸面朝上还是朝下
         无关，后出来的纸总是压在先出来的纸上；
      3. 第二次进纸前整叠翻面会让纸叠顺序反转，不翻则保持原样。

    用户翻不翻面的判据是"让空白面朝向打印面"，所以装回后必定印得上，
    与进纸盒要求哪面朝上无关——这也正是页序不能由出纸方向推出来的原因。
    """
    print("\n页序 · 纸张流转模拟（不看实现，从物理过程推）")
    for flip in (FLIP_NONE, FLIP_LONG_EDGE, FLIP_SHORT_EDGE):
        wrong: list[str] = []
        for n in (1, 2, 5, 6, 7, 26):
            front, back = page_orders(n, flip)

            # 阶段1：按正面文件顺序进纸，第 i 张纸印上源页 front[i]；
            #        出纸堆叠后最后打印的压在最上面。
            stack = list(reversed(front))
            # 阶段2：整叠翻面会把纸叠顺序反转过来
            if flip != FLIP_NONE:
                stack.reverse()
            # 阶段3：按纸叠顺序再次进纸，依次印上反面文件各页
            printed = dict(zip(stack, back))

            for src_page in front:
                # 0-based 源页 src_page 的背面，就是同一张纸的另一面
                want = src_page + 1 if src_page + 1 < n else None
                got = printed.get(src_page)
                if got != want:
                    wrong.append(f"n={n} 源第{src_page + 1}页背面={got} 应为{want}")
        check(f"{flip} 每张纸正反面配对正确", not wrong, "; ".join(wrong[:3]))


def test_rotation_policy() -> None:
    """锁死"哪种翻面方式要预转 180°"这个因机而异的取值。

    这条规则散落在生成、文案、动画、拼合预览四处，改一次很容易只改一半
    （实际发生过）。取值本身是实测的，改它必须是有意识的决定——所以钉在这里。
    """
    print("\n旋转策略 · 左右反转预转 180°、上下翻转不转（按实测）")
    check("左右反转需要预转 180°", back_needs_rotation(FLIP_LONG_EDGE) is True)
    check("上下翻转不预转", back_needs_rotation(FLIP_SHORT_EDGE) is False)
    check("不翻面不预转", back_needs_rotation(FLIP_NONE) is False)


def test_odd_page_placeholder(tmpdir: str) -> None:
    """奇数页文档：不翻面时必须补一张空白占位页，翻面时不能补。

    占位页补错位置是最隐蔽的一类错误——后面所有偶数页会整体错一张纸，
    打出来每一张纸的正反面都对不上。
    """
    print("\n页序 · 奇数页文档的空白占位页")
    src_path = os.path.join(tmpdir, "odd5.pdf")
    doc = fitz.open()
    for i in range(5):
        doc.new_page().insert_text((72, 72), f"PAGE-{i + 1}", fontsize=24)
    doc.save(src_path)
    doc.close()

    expect = {
        # flip: (反面页数, 第 1 页是否为空白占位页)
        FLIP_NONE: (3, True),         # [空白, p4, p2]
        FLIP_LONG_EDGE: (2, False),   # [p2, p4]，且每页旋转 180°
        FLIP_SHORT_EDGE: (2, False),  # [p2, p4]，不旋转
    }
    for flip, (want_pages, want_blank_first) in expect.items():
        front, back = DuplexReorderer().reorder(src_path, flip=flip)
        with fitz.open(stream=front, filetype="pdf") as fd, \
                fitz.open(stream=back, filetype="pdf") as bd:
            check(f"{flip} 正面 3 页（1/3/5）", fd.page_count == 3, f"实际 {fd.page_count}")
            check(f"{flip} 反面 {want_pages} 页", bd.page_count == want_pages,
                  f"实际 {bd.page_count}")
            texts = [pg.get_text().strip() for pg in bd]
            check(f"{flip} 第 1 页{'是空白占位' if want_blank_first else '不是空白'}",
                  (texts[0] == "") == want_blank_first, f"实际 {texts!r}")
            if not want_blank_first:
                check(f"{flip} 反面页序为 2、4", texts == ["PAGE-2", "PAGE-4"],
                      f"实际 {texts!r}")


def gray(page: fitz.Page, dpi: int = 100) -> Image.Image:
    return Image.open(
        io.BytesIO(page.get_pixmap(dpi=dpi, alpha=False).tobytes("png"))
    ).convert("L")


def similar(a: Image.Image, b: Image.Image, tolerance: int = 30, ratio: float = 0.01) -> bool:
    """模糊后比对整页（容忍 180° 旋转带来的亚像素栅格化差异）。

    注意 histogram() 对 RGB 图是按通道拼接的，必须先转成灰度再取阈值切片。
    """
    if a.size != b.size:
        return False
    a = a.filter(ImageFilter.GaussianBlur(2.0))
    b = b.filter(ImageFilter.GaussianBlur(2.0))
    histogram = ImageChops.difference(a, b).histogram()
    return sum(histogram[tolerance:]) / (a.size[0] * a.size[1]) < ratio


def image_samples(doc: fitz.Document, xref: int) -> np.ndarray:
    pix = fitz.Pixmap(doc, xref)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)


def compose(m: tuple, n: tuple) -> tuple:
    """矩阵复合 m∘n（先经 n 变换，再经 m）。"""
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a*A + b*C, a*B + b*D, c*A + d*C, c*B + d*D, a*E + b*F + e, c*E + d*F + f)


_TOKEN_RE = re.compile(
    rb"\((?:[^()\\]|\\.)*\)"      # 字符串
    rb"|<[^>]*>"                  # 字典/十六进制串
    rb"|/[^\s/<>\[\]()]+"         # 名字
    rb"|[-+]?(?:\d+\.?\d*|\.\d+)"  # 数字
    rb"|[A-Za-z*'\"]+"            # 运算符
)


def draw_matrices(content: bytes):
    """走一遍内容流，产出每次 Do 时的 (资源名, CTM)。

    CTM 是逐步复合出来的——这正是打印机/渲染器在那一刻看到的矩阵，
    比用正则去猜"哪个 cm 配哪个 Do"可靠。
    """
    ctm = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    saved: list[tuple] = []
    operands: list[float] = []
    name = ""
    for token in _TOKEN_RE.findall(content):
        head = token[:1]
        if head == b"/":
            name = token[1:].decode("latin-1")
            continue
        if head in (b"(", b"<"):
            continue
        try:
            operands.append(float(token))
            continue
        except ValueError:
            pass
        if token == b"cm" and len(operands) >= 6:
            ctm = compose(ctm, tuple(operands[-6:]))
        elif token == b"q":
            saved.append(ctm)
        elif token == b"Q":
            if saved:
                ctm = saved.pop()
        elif token == b"Do":
            yield name, ctm
        operands.clear()  # 只有运算符才会清空操作数栈


def test_geometry(sample: str) -> None:
    """缺陷1：反面页的 180° 旋转不依赖 /Rotate，几何必须精确。"""
    print(f"\n缺陷1 · 反面旋转写进内容流（{os.path.basename(sample)}）")
    _, back = DuplexReorderer().reorder(sample, flip=FLIP_LONG_EDGE)
    check("反面文件非空", bool(back))
    if not back:
        return

    src = fitz.open(sample)
    out = fitz.open(stream=back, filetype="pdf")
    try:
        check("输出页 /Rotate 全为 0（不再依赖显示标志位）",
              {p.rotation for p in out} == {0}, f"实际 {sorted({p.rotation for p in out})}")

        order = back_order(src.page_count, FLIP_LONG_EDGE)
        worst = 0.0
        for i in range(out.page_count):
            page = src[order[i]]
            w, h = page.rect.width, page.rect.height
            expect = sorted(
                (round(w - x[2], 2), round(h - x[3], 2), round(w - x[0], 2), round(h - x[1], 2), x[4])
                for x in page.get_text("words")
            )
            got = sorted(
                (round(x[0], 2), round(x[1], 2), round(x[2], 2), round(x[3], 2), x[4])
                for x in out[i].get_text("words")
            )
            check(f"第{i+1}页词数一致", len(expect) == len(got), f"{len(expect)} vs {len(got)}")
            for e, g in zip(expect, got):
                if e[4] == g[4]:
                    worst = max(worst, max(abs(e[j] - g[j]) for j in range(4)))
        check("文字坐标 = 源页旋转 180°", worst < 0.5, f"最大偏差 {worst:.3f} pt")
    finally:
        src.close()
        out.close()


def test_image_matrices(sample: str) -> None:
    """缺陷2：位图的有效变换矩阵必须回到"正的纯缩放平移"。"""
    print("\n缺陷2 · 位图有效矩阵回正（打印机能正常处理的形式）")
    _, back = DuplexReorderer().reorder(sample, flip=FLIP_LONG_EDGE)
    src = fitz.open(sample)
    out = fitz.open(stream=back, filetype="pdf")
    try:
        order = back_order(src.page_count, FLIP_LONG_EDGE)
        total = 0
        bad = []
        for i in range(out.page_count):
            resources = _resource_map(out, out[i].xref, "XObject")
            for name, ctm in draw_matrices(out[i].read_contents()):
                xref = resources.get(name)
                if xref is None or _key(out, xref, "Subtype") != "/Image":
                    continue
                total += 1
                # 打印机在这一次 Do 上看到的矩阵：正、无旋转、无翻转
                if not (ctm[0] > 0 and ctm[3] > 0
                        and abs(ctm[1]) < 1e-6 and abs(ctm[2]) < 1e-6):
                    bad.append((i + 1, name, ctm))
        source_images = sum(len(p.get_images(full=True)) for p in src)
        if source_images:
            check("源文档的位图都被处理到", total > 0,
                  f"源位图 {source_images} 个 → 处理 {total} 处绘制")
        else:
            print(f"  [INFO] 样本没有位图，矩阵检查跳过（共 {total} 处绘制）")
        check("每处位图的有效矩阵都是正的纯缩放平移", not bad, str(bad[:3]))

        # 位图采样必须等于源位图旋转 180°，且落位一致
        wrong = []
        for i in range(out.page_count):
            page = src[order[i]]
            w, h = page.rect.width, page.rect.height
            boxes = {im[0]: page.get_image_bbox(im) for im in page.get_images(full=True)}
            for xref, box in {im[0]: out[i].get_image_bbox(im)
                              for im in out[i].get_images(full=True)}.items():
                want = fitz.Rect(w - box.x1, h - box.y1, w - box.x0, h - box.y0)
                hit = [x for x, b in boxes.items()
                       if abs(b.x0 - want.x0) < 0.5 and abs(b.y0 - want.y0) < 0.5]
                if not hit:
                    wrong.append(f"第{i+1}页 xref={xref} 落位对不上")
                    continue
                a = image_samples(out, xref)
                b = image_samples(src, hit[0])[::-1, ::-1]
                if a.shape != b.shape or not np.array_equal(a, b):
                    wrong.append(f"第{i+1}页 xref={xref} 采样不符")
        check("位图采样 = 源位图旋转 180°，落位一致", not wrong, "; ".join(wrong[:3]))
    finally:
        src.close()
        out.close()


def test_pixels(sample: str) -> None:
    """整页渲染必须等于源页旋转 180°。"""
    print("\n缺陷3 · 整页渲染等价")
    _, back = DuplexReorderer().reorder(sample, flip=FLIP_LONG_EDGE)
    src = fitz.open(sample)
    out = fitz.open(stream=back, filetype="pdf")
    try:
        order = back_order(src.page_count, FLIP_LONG_EDGE)
        worst = 0.0
        for i in range(out.page_count):
            if not similar(gray(out[i]), gray(src[order[i]]).rotate(180)):
                worst = 1.0
                check(f"第{i+1}页渲染", False)
        check("每页渲染 ≈ 源页旋转 180°", worst == 0.0)
    finally:
        src.close()
        out.close()


def test_no_rotation(sample: str) -> None:
    """不需要旋转时，正面/反面都必须是原样拷贝。"""
    print("\n缺陷4 · 不旋转时内容原样保留")
    front, back = DuplexReorderer().reorder(sample, flip=FLIP_SHORT_EDGE)
    src = fitz.open(sample)
    try:
        order = back_order(src.page_count, FLIP_SHORT_EDGE)
        ok_content = all(
            bytes(back_doc) == bytes(src[i].read_contents())
            for back_doc, i in zip(_pages(back), order)
        )
        check("反面页内容与源页逐字节相同", ok_content)
        ok_front = all(
            bytes(front_doc) == bytes(src[i].read_contents())
            for front_doc, i in zip(_pages(front), range(0, src.page_count, 2))
        )
        check("正面页内容与源页逐字节相同", ok_front)
    finally:
        src.close()


def _pages(data: bytes):
    doc = fitz.open(stream=data, filetype="pdf")
    for page in doc:
        yield page.read_contents()


def test_raster_fallback(sample: str) -> None:
    """兜底：位图无法安全旋转时，整页光栅化输出，结果依然正确。"""
    print("\n缺陷5 · 位图无法旋转时的整页光栅化兜底")
    original = reorderer_module._rotate_image_180
    reorderer_module._rotate_image_180 = lambda *a, **k: False
    try:
        _, back = DuplexReorderer().reorder(sample, flip=FLIP_LONG_EDGE)
    finally:
        reorderer_module._rotate_image_180 = original

    src = fitz.open(sample)
    out = fitz.open(stream=back, filetype="pdf")
    try:
        order = back_order(src.page_count, FLIP_LONG_EDGE)
        check("输出页数不变", out.page_count == len(order), f"{out.page_count} vs {len(order)}")
        counts = [len(p.get_images(full=True)) for p in out]
        check("每页都被压成单张位图", all(c == 1 for c in counts), f"每页图片数 {counts}")
        # 100dpi 时 300dpi 的光栅图正好 3 倍整数缩放，取样最干净
        worst = 0.0
        for i in range(out.page_count):
            a = gray(out[i], 100).filter(ImageFilter.GaussianBlur(2.0))
            b = gray(src[order[i]], 100).rotate(180).filter(ImageFilter.GaussianBlur(2.0))
            histogram = ImageChops.difference(a, b).histogram()
            worst = max(worst, sum(histogram[30:]) / (a.size[0] * a.size[1]))
        # 经过"光栅化 + JPEG 有损 + 重采样"，容忍度比矢量页高一个量级
        check("光栅化后内容仍 ≈ 源页旋转 180°", worst < 0.02, f"最大差异像素占比 {worst:.4f}")
    finally:
        src.close()
        out.close()


def test_widget_pairing(sample: str) -> None:
    print("\n缺陷6 · 界面翻面方式 → 页序、旋转与文案")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication, QFileDialog

    app = QApplication.instance() or QApplication([])
    from tools.duplex_print.ui import widget as widget_module

    # 生成完成后的成功弹窗是模态的，离屏环境里会一直阻塞，测试中置空
    widget_module.show_success = lambda *a, **k: None

    widget = widget_module.DuplexPrintWidget()
    widget._load_pdf(sample)
    n = widget._page_count

    cases = [
        ("不翻面", widget.radio_flip_none, FLIP_NONE, False, "不要翻面"),
        ("左右翻", widget.radio_flip_long, FLIP_LONG_EDGE, True, "左右反转"),
        ("上下翻", widget.radio_flip_short, FLIP_SHORT_EDGE, False, "上下翻转"),
    ]
    for label, radio, expect_flip, expect_rotate, expect_word in cases:
        radio.setChecked(True)
        app.processEvents()

        # 预览页序必须与重排核心算出来的完全一致（预览与实际对不上是致命伤）
        front_ui, back_ui = widget._front_back_orders()
        front_core, back_core = page_orders(n, expect_flip)
        check(f"{label} 预览页序与重排核心一致",
              front_ui == [p + 1 for p in front_core]
              and back_ui == [None if p is None else p + 1 for p in back_core],
              f"预览 正{front_ui} 反{back_ui}")

        captured: dict[str, str] = {}
        original = DuplexReorderer.reorder

        def spy(self, path, flip=FLIP_LONG_EDGE, _c=captured, _o=original):
            _c["flip"] = flip
            return _o(self, path, flip=flip)

        DuplexReorderer.reorder = spy
        try:
            with tempfile.TemporaryDirectory() as tmp:
                target = os.path.join(tmp, "out.pdf")
                QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (target, ""))
                widget._on_generate()
        finally:
            DuplexReorderer.reorder = original

        check(f"{label} → 传给重排核心 flip={expect_flip}",
              captured.get("flip") == expect_flip, f"实际 {captured.get('flip')}")
        check(f"{label} 步骤文案与动作一致", expect_word in widget.label_steps.text())

    # 反面页旋转只在"左右翻"时发生，预览标题要跟着标
    widget.radio_flip_long.setChecked(True)
    app.processEvents()
    check("左右翻时预览标题标注旋转", "每页旋转 180°" in widget.label_back_title.text())
    widget.radio_flip_short.setChecked(True)
    app.processEvents()
    check("上下翻时预览标题不标注旋转", "每页旋转 180°" not in widget.label_back_title.text())

    # 出纸方式只负责预选推荐的翻面方式，不参与页序计算。
    # 注意要真的切换它的取值——已经选中的 radio 再 setChecked(True) 不发信号。
    widget.radio_flip_short.setChecked(True)
    widget.radio_face_up.setChecked(True)
    app.processEvents()
    check("选「印好一面朝上」预选「左右翻」", widget.radio_flip_long.isChecked())
    widget.radio_flip_short.setChecked(True)
    widget.radio_face_down.setChecked(True)
    app.processEvents()
    check("选「印好一面朝下」预选「不翻面」", widget.radio_flip_none.isChecked())


def main() -> None:
    sample = sys.argv[1] if len(sys.argv) > 1 else next(
        (p for p in SAMPLE_CANDIDATES if os.path.exists(p)), None
    )
    if not sample or not os.path.exists(sample):
        print(f"找不到样本 PDF，请显式传入路径。候选：{SAMPLE_CANDIDATES}")
        raise SystemExit(1)
    print(f"样本: {sample}")

    test_paper_flow()
    test_rotation_policy()
    with tempfile.TemporaryDirectory() as tmp:
        test_odd_page_placeholder(tmp)
    test_geometry(sample)
    test_image_matrices(sample)
    test_pixels(sample)
    test_no_rotation(sample)
    test_raster_fallback(sample)
    test_widget_pairing(sample)

    print("\n" + "=" * 60)
    if failures:
        print(f"失败 {len(failures)} 项:")
        for item in failures:
            print(f"  - {item}")
        raise SystemExit(1)
    print("全部通过")


if __name__ == "__main__":
    main()

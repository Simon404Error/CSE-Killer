"""手动双面打印 · 端到端验证：按界面选项生成 → 按物理流程把纸合回来。

与同目录 regression_duplex.py 的区别：那个脚本写的是另一套尚未落地的 API
（page_orders / FLIP_NONE / FLIP_LONG_EDGE / FLIP_SHORT_EDGE、三选一的翻面
单选 radio_flip_none/long/short），当前代码里没有，import 即失败。本脚本只
测当前工作区的实现：reorder(face_down, flip_short_edge) + radio_face_*/radio_same|opposite。

验证分三层：
    A. 文件级——页序、180° 旋转、/Rotate 标志、位图有效矩阵、整页渲染；
    B. 纸张流转——按界面「三步打印」文字里写的动作模拟进纸→堆叠→翻面→再进纸，
       逐张纸核对正反面是否配对（这是"合起来有没有问题"的直接答案）；
    C. 组合矩阵——4 组选项 × 奇/偶页数，找出哪些组合自相矛盾。

用法:
    python -u tools/duplex_print/dev/verify_manual_duplex.py [真实样本PDF]
"""

import io
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fitz  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageChops, ImageFilter  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}", flush=True)
    if not ok:
        failures.append(label)
    return ok


# ============================================================
#  样本构造
# ============================================================

def _asymmetric_png(k: int) -> bytes:
    """造一张 180° 旋转后必然对不上的位图，且每页图案不同（串页也能看出来）。"""
    img = Image.new("RGB", (120, 100), "white")
    px = img.load()
    for y in range(100):
        for x in range(120):
            if x < 14 + k * 2 and y < 8 + k * 3:
                px[x, y] = (220, 30, 30)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def build_sample(path: str, pages: int) -> None:
    """合成样本：每页一个角标文字 + 一张非对称位图。A4 纵向。"""
    doc = fitz.open()
    for k in range(1, pages + 1):
        page = doc.new_page(width=595, height=842)
        page.insert_text((60, 70), f"PAGE-{k}", fontsize=28)
        page.insert_image(fitz.Rect(430, 640, 550, 740), stream=_asymmetric_png(k))
    doc.save(path)
    doc.close()


# ============================================================
#  内容流解析：拿到每次 Do 时的有效变换矩阵
# ============================================================

_TOKEN_RE = re.compile(
    rb"\((?:[^()\\]|\\.)*\)"      # 字符串
    rb"|<[^>]*>"                  # 字典/十六进制串
    rb"|/[^\s/<>\[\]()]+"         # 名字
    rb"|[-+]?(?:\d+\.?\d*|\.\d+)"  # 数字
    rb"|[A-Za-z*'\"]+"            # 运算符
)
_REF_RE = re.compile(r"(\d+)\s+0\s+R")
_RES_RE = re.compile(r"/([\w.#\-]+)\s+(\d+)\s+0\s+R")


def compose(m: tuple, n: tuple) -> tuple:
    """矩阵复合 m∘n（先经 n 变换，再经 m）。"""
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + b * C, a * B + b * D, c * A + d * C, c * B + d * D, a * E + b * F + e, c * E + d * F + f)


def draw_matrices(content: bytes):
    """走一遍内容流，产出每次 Do 时的 (资源名, 有效矩阵 CTM)。

    矩阵是逐步复合出来的——这正是打印机/渲染器在那一刻看到的东西，
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
        operands.clear()


def resource_map(doc: fitz.Document, owner_xref: int, category: str) -> dict[str, int]:
    """读页面/Form 的资源字典：{资源名: 对象xref}。"""
    kind, value = doc.xref_get_key(owner_xref, f"Resources/{category}")
    if kind == "null" or not value:
        return {}
    if kind in ("xref", "array"):
        found = _REF_RE.search(value)
        if not found:
            return {}
        value = doc.xref_object(int(found.group(1)))
    return {name: int(xref) for name, xref in _RES_RE.findall(value)}


def xobject_map(page: fitz.Page) -> dict[str, int]:
    """页面资源里的 XObject 映射。get_xobjects() 在部分版本/文件上给空列表，
    所以拿到空就退回自己解析资源字典。"""
    try:
        out = {}
        for entry in page.get_xobjects():
            name = entry[1]
            if isinstance(name, bytes):
                name = name.decode("latin-1")
            if name:
                out[name] = entry[0]
        if out:
            return out
    except Exception:  # noqa: BLE001 - 忽略，走下面的资源字典解析
        pass
    return resource_map(page.parent, page.xref, "XObject")


def key(doc: fitz.Document, xref: int, k: str) -> str | None:
    kind, value = doc.xref_get_key(xref, k)
    return None if kind == "null" else value


def image_draws(page: fitz.Page) -> list[tuple[str, int, tuple]]:
    """页面上画到的位图：[(资源名, 位图xref, 有效矩阵)]。"""
    xobjects = xobject_map(page)
    out = []
    for name, ctm in draw_matrices(page.read_contents()):
        xref = xobjects.get(name)
        if xref is not None and key(page.parent, xref, "Subtype") == "/Image":
            out.append((name, xref, ctm))
    return out


# ============================================================
#  页面比对
# ============================================================

def gray(page: fitz.Page, dpi: int = 100) -> Image.Image:
    return Image.open(io.BytesIO(page.get_pixmap(dpi=dpi, alpha=False).tobytes("png"))).convert("L")


def diff_ratio(a: Image.Image, b: Image.Image, tolerance: int = 30) -> float:
    """模糊后比对整页，返回差异像素占比（容忍 180° 旋转的亚像素栅格化差异）。"""
    if a.size != b.size:
        return 1.0
    a = a.filter(ImageFilter.GaussianBlur(2.0))
    b = b.filter(ImageFilter.GaussianBlur(2.0))
    histogram = ImageChops.difference(a, b).histogram()
    return sum(histogram[tolerance:]) / (a.size[0] * a.size[1])


def marker_centers(doc: fitz.Document) -> dict[int, tuple[float, float]]:
    """{源页号: 角标中心}。"""
    out = {}
    for page in doc:
        for w in page.get_text("words"):
            if w[4].startswith("PAGE-"):
                out[int(w[4].split("-")[1])] = ((w[0] + w[2]) / 2, (w[1] + w[3]) / 2)
    return out


def marker_state(page: fitz.Page, centers: dict[int, tuple[float, float]]) -> tuple[int, bool, float]:
    """读合成样本的角标：返回 (源页号, 是否被旋转 180°, 与理论落点的偏差)。

    理论落点：未旋转 = 源页的角标中心；旋转 180° = 中心关于页面中心对称。
    """
    words = [w for w in page.get_text("words") if w[4].startswith("PAGE-")]
    if not words:
        return 0, False, -1.0
    w = words[0]
    num = int(w[4].split("-")[1])
    cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
    W, H = page.rect.width, page.rect.height
    sx, sy = centers[num]
    to_source = max(abs(cx - sx), abs(cy - sy))
    to_flipped = max(abs(cx - (W - sx)), abs(cy - (H - sy)))
    return num, to_flipped < to_source, min(to_source, to_flipped)


def identify_all(page: fitz.Page, src_doc: fitz.Document, dpi: int = 60) -> list[tuple[int, bool]]:
    """暴力识别：这一页可能是源文档第几页、有没有旋转 180°。

    先按"逐像素完全相同"找——同一页在同一环境下重新渲染必然逐像素相同；
    找不到才放宽到近似匹配。不能只按近似匹配：试卷里同一套图表配不同题目时，
    两页能像到模糊后完全没差别（实测 60dpi 只差 57 个点），照近似匹配会认错页。
    """
    got = gray(page, dpi)
    raw_got = page.get_pixmap(dpi=dpi, alpha=False).samples
    exact: list[tuple[int, bool]] = []
    loose: list[tuple[int, bool]] = []
    for index, src_page in enumerate(src_doc):
        if src_page.get_pixmap(dpi=dpi, alpha=False).samples == raw_got:
            exact.append((index + 1, False))
            continue
        # 旋转过的页面没法要求逐像素相同（重采样有亚像素差），只能近似比对
        if diff_ratio(got, gray(src_page, dpi).rotate(180)) < 0.01:
            loose.append((index + 1, True))
    return exact or loose


# ============================================================
#  走真实控件路径生成
# ============================================================

def open_doc(path: str) -> fitz.Document:
    return fitz.open(path)


def _raise_dialog(*args, **kwargs):
    """把控件里的错误弹窗变成异常——离屏环境里模态框会一直阻塞。"""
    raise RuntimeError(args[2] if len(args) > 2 else "控件弹出错误对话框")


def drive_ui(src: str, out_dir: str, tag: str, face_down: bool, rotate: bool) -> dict:
    """走真实控件路径：设选项 → 生成两份文件 → 回读预览页序与三步说明。"""
    from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox

    from tools.duplex_print.ui import widget as widget_module

    app = QApplication.instance() or QApplication([])
    widget_module.show_success = lambda *a, **k: None   # 模态弹窗在离屏环境会卡住
    QMessageBox.critical = staticmethod(_raise_dialog)
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)

    widget = widget_module.DuplexPrintWidget()
    try:
        widget._load_pdf(src)
        widget.radio_face_down.setChecked(face_down)
        widget.radio_face_up.setChecked(not face_down)
        widget.radio_same.setChecked(not rotate)
        widget.radio_opposite.setChecked(rotate)
        widget._refresh_preview()
        widget._refresh_steps()
        app.processEvents()

        preview_front, preview_back = widget._front_back_orders()
        steps = widget.label_steps.text()

        # 每次用独立的基准名，避免"已存在是否覆盖"的确认框
        base = os.path.join(out_dir, f"{tag}.pdf")
        QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (base, ""))
        widget._on_generate()
    finally:
        widget.deleteLater()
        app.processEvents()

    return {
        "front": f"{out_dir}/{tag}-正面.pdf",
        "back": f"{out_dir}/{tag}-反面.pdf",
        "preview_front": preview_front,
        "preview_back": preview_back,
        "steps": steps,
    }


# ============================================================
#  纸张流转模拟
# ============================================================

def simulate_pairing(front: list[int], back: list[int], action: str) -> dict[int, int | None]:
    """把纸走一遍：返回 {正面源页号: 反面源页号}（None = 这张纸反面空白）。

    action: "none" 不翻面 / "long" 左右反转（长边为轴）/ "short" 上下翻转（短边为轴）
    只依赖三条与机型无关的事实：
      1. 进纸盒从纸叠最上面逐张取纸；
      2. 后出来的纸压在先出来的纸上（出纸面朝上还是朝下都一样）；
      3. 整叠翻面（绕哪条轴都一样）会让纸叠顺序反转，不翻则保持原样。
    """
    stack = list(reversed(front))          # 出纸堆叠：最后打印的在最上面
    if action != "none":
        stack.reverse()                    # 整叠翻面 → 顺序反转
    padded = list(back) + [None] * (len(stack) - len(back))
    return dict(zip(stack, padded))


def expected_pairing(n: int) -> dict[int, int | None]:
    """正确答案：源页 2i+1 的背面必须是 2i+2（不存在则为空白）。"""
    return {p: (p + 1 if p + 1 <= n else None) for p in range(1, n + 1, 2)}


def prescribed_action(step_text: str) -> str:
    """从界面「三步打印」的说明文字里读出它要求的翻面动作。"""
    if "上下翻转" in step_text:
        return "short"
    if "左右反转" in step_text:
        return "long"
    return "none"


def describe_bad(got: dict[int, int | None], want: dict[int, int | None]) -> str:
    bad = [f"第{p}页背面={got.get(p)} 应为{want[p]}" for p in want if got.get(p) != want[p]]
    return "; ".join(bad[:4]) + (f"（共 {len(bad)} 处）" if len(bad) > 4 else "")


# ============================================================
#  A 层：文件级检查
# ============================================================

def verify_files(label: str, src: str, front_path: str, back_path: str,
                 n: int, face_down: bool, rotate: bool) -> tuple[list[int], list[int]]:
    """检查两份文件的内容，返回 (正面源页序, 反面源页序)。"""
    print(f"\n{label}")
    want_front = list(range(1, n + 1, 2))
    want_back = list(range(2, n + 1, 2))
    if face_down:
        want_back.reverse()

    src_doc = open_doc(src)
    front_doc = open_doc(front_path)
    back_doc = open_doc(back_path)
    try:
        centers = marker_centers(src_doc)
        if centers:
            front_state = [marker_state(p, centers) for p in front_doc]
            back_state = [marker_state(p, centers) for p in back_doc]
            front_pages = [s[0] for s in front_state]
            back_pages = [s[0] for s in back_state]
            check(f"正面 {len(want_front)} 页，页序 {want_front}", front_pages == want_front, f"实际 {front_pages}")
            check(f"反面 {len(want_back)} 页，页序 {want_back}", back_pages == want_back, f"实际 {back_pages}")
            check("正面页均未旋转", all(not s[1] for s in front_state), f"实际 {[s[1] for s in front_state]}")
            check(f"反面页{'均已旋转 180°' if rotate else '均未旋转'}",
                  all(s[1] == rotate for s in back_state), f"实际 {[s[1] for s in back_state]}")
            worst = max(s[2] for s in front_state + back_state)
            check("角标落点精确（不是部分旋转）", worst < 1.0, f"最大偏差 {worst:.2f} pt")
        else:
            # 真实样本没有角标：用渲染暴力识别最前面几页，确认页序与旋转确实如预期
            front_pages, back_pages = want_front, want_back
            detail = []
            wrong = []
            for what, page, expect in (("正面第1页", front_doc[0], (want_front[0], False)),
                                       ("反面第1页", back_doc[0], (want_back[0], rotate)),
                                       ("反面第2页", back_doc[1], (want_back[1], rotate))):
                candidates = identify_all(page, src_doc)
                detail.append(f"{what}候选 {candidates[:3]}")
                if expect not in candidates:
                    wrong.append(f"{what}: 预期 {expect}，候选 {candidates[:3]}")
            check("渲染识别出的页序与旋转符合预期", not wrong, "; ".join(wrong) if wrong else "；".join(detail))

        check("两份文件所有页 /Rotate = 0（不靠显示标志位）",
              all(p.rotation == 0 for p in front_doc) and all(p.rotation == 0 for p in back_doc),
              f"正{sorted({p.rotation for p in front_doc})} 反{sorted({p.rotation for p in back_doc})}")

        # 整页渲染：反面第 i 页 == 源页 rotate180（旋转时）/ 原样（不旋转时）
        worst_ratio = 0.0
        for i, page in enumerate(back_doc):
            want = gray(src_doc[want_back[i] - 1])
            if rotate:
                want = want.rotate(180)
            worst_ratio = max(worst_ratio, diff_ratio(gray(page), want))
        check(f"反面每页渲染 = 源页{'旋转 180°' if rotate else '原样'}",
              worst_ratio < 0.01, f"最大差异像素占比 {worst_ratio:.4f}")

        # 正面文件必须是原样拷贝（旋转不旋转都不该动它）
        if centers:
            same = all(bytes(out.read_contents()) == bytes(src_doc[i].read_contents())
                       for out, i in zip(front_doc, range(0, n, 2)))
            check("正面文件内容与源页逐字节相同", same)

        # 位图有效矩阵：必须回到"正的纯缩放平移"——这是打印机贴错位图的那条红线
        draws = [d for page in back_doc for d in image_draws(page)]
        bad_matrix = [(name, tuple(round(v, 3) for v in ctm)) for name, _, ctm in draws
                      if not (ctm[0] > 0 and ctm[3] > 0 and abs(ctm[1]) < 1e-6 and abs(ctm[2]) < 1e-6)]
        if draws:
            check("反面位图有效矩阵全为正的纯缩放平移", not bad_matrix,
                  f"共 {len(draws)} 处绘制" + (f"，坏矩阵 {bad_matrix[:3]}" if bad_matrix else
                                              f"，例 {draws[0][0]} {tuple(round(v, 2) for v in draws[0][2])}"))
        else:
            print("  [INFO] 反面没有位图绘制，矩阵检查跳过")

        # 位图采样：必须是源位图旋转 180°，且落位一致
        if rotate and draws:
            wrong = []
            for i, page in enumerate(back_doc[:3]):
                src_page = src_doc[want_back[i] - 1]
                W, H = src_page.rect.width, src_page.rect.height
                src_boxes = {im[0]: src_page.get_image_bbox(im) for im in src_page.get_images(full=True)}
                for info in page.get_image_info(xrefs=True):
                    box = fitz.Rect(info["bbox"])
                    want_box = fitz.Rect(W - box.x1, H - box.y1, W - box.x0, H - box.y0)
                    hit = [x for x, b in src_boxes.items()
                           if abs(b.x0 - want_box.x0) < 0.5 and abs(b.y0 - want_box.y0) < 0.5]
                    if not hit:
                        wrong.append(f"第{i+1}页位图落位对不上")
                        continue
                    a = fitz.Pixmap(back_doc, info["xref"])
                    b = fitz.Pixmap(src_doc, hit[0])
                    pa = np.frombuffer(a.samples, dtype=np.uint8).reshape(a.height, a.width, a.n)
                    pb = np.frombuffer(b.samples, dtype=np.uint8).reshape(b.height, b.width, b.n)[::-1, ::-1]
                    if pa.shape != pb.shape or not np.array_equal(pa, pb):
                        wrong.append(f"第{i+1}页位图采样不符")
            check("位图采样 = 源位图旋转 180°，落位一致", not wrong, "; ".join(wrong[:3]))
    finally:
        src_doc.close()
        front_doc.close()
        back_doc.close()

    return front_pages, back_pages


# ============================================================
#  C 层：组合矩阵
# ============================================================

COMBOS = [
    ("印好一面朝下（激光）", "同向 · 不旋转", True, False),
    ("印好一面朝下（激光）", "相反 · 旋转 180°", True, True),
    ("印好一面朝上（喷墨）", "同向 · 不旋转", False, False),
    ("印好一面朝上（喷墨）", "相反 · 旋转 180°", False, True),
]


def matrix(tmp: str) -> None:
    print("\n" + "=" * 72)
    print("组合矩阵：4 组选项 × 奇数/偶数页数（合成样本）")
    print("=" * 72)
    for n in (6, 5):
        sample = os.path.join(tmp, f"sample{n}.pdf")
        build_sample(sample, n)
        with open_doc(sample) as sample_doc:
            centers = marker_centers(sample_doc)
        for index, (out_label, rotate_label, face_down, rotate) in enumerate(COMBOS):
            title = f"印好一面朝{'下' if face_down else '上'} + {rotate_label}（{n} 页）"
            result = drive_ui(sample, tmp, f"m{n}_{index}", face_down, rotate)
            with open_doc(result["front"]) as fd, open_doc(result["back"]) as bd:
                front = [marker_state(p, centers)[0] for p in fd]
                back = [marker_state(p, centers)[0] for p in bd]

            want = expected_pairing(n)
            text_action = prescribed_action(result["steps"])
            physical = "none" if face_down else "long"  # 出纸方式本身要求的动作
            by_text = simulate_pairing(front, back, text_action)
            by_physical = simulate_pairing(front, back, physical)

            print(f"\n  ── {title}")
            print(f"     界面文字要求：{'上下翻转' if text_action == 'short' else '左右反转' if text_action == 'long' else '不翻面'}"
                  f"；出纸方式的物理要求：{'不翻面' if physical == 'none' else '翻面（绕哪条轴都行）'}")
            check(f"{title}：照界面文字执行 → 配对", by_text == want, describe_bad(by_text, want))
            if by_physical != by_text:
                check(f"{title}：按出纸方式的物理要求执行 → 配对",
                      by_physical == want, describe_bad(by_physical, want))


# ============================================================
#  真实样本
# ============================================================

def real_sample(path: str, tmp: str) -> None:
    print("\n" + "=" * 72)
    print(f"真实样本：{os.path.basename(path)}")
    print("=" * 72)
    src = open_doc(path)
    n = src.page_count
    image_count = sum(len(p.get_images(full=True)) for p in src)
    src.close()
    print(f"共 {n} 页，位图 {image_count} 个\n")

    for index, (out_label, rotate_label, face_down, rotate) in enumerate(COMBOS):
        label = f"【{out_label} + {rotate_label}】"
        result = drive_ui(path, tmp, f"real{index}", face_down, rotate)
        front, back = verify_files(label, path, result["front"], result["back"], n, face_down, rotate)
        want = expected_pairing(n)
        text_action = prescribed_action(result["steps"])
        got = simulate_pairing(front, back, text_action)
        check(f"{label} 照界面文字执行（{'不翻面' if text_action == 'none' else '翻面'}）→ 配对",
              got == want, describe_bad(got, want))


def main() -> None:
    user_sample = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "合并.pdf")

    with tempfile.TemporaryDirectory() as tmp:
        # ---------- A 层：用户指定的组合 ----------
        print("=" * 72)
        print("A. 用户指定组合：印好一面朝上（多数喷墨） + 反面文字与正面相反 · 旋转 180°")
        print("=" * 72)

        n = 6
        sample = os.path.join(tmp, f"sample{n}.pdf")
        build_sample(sample, n)
        result = drive_ui(sample, tmp, "a0", face_down=False, rotate=True)
        front, back = verify_files(f"A0 合成 {n} 页样本", sample, result["front"], result["back"],
                                   n, face_down=False, rotate=True)
        check("界面预览页序 = 实际生成文件页序",
              result["preview_front"] == front and result["preview_back"] == back,
              f"预览 正{result['preview_front']} 反{result['preview_back']}")
        action = prescribed_action(result["steps"])  # 用户会照做的动作
        print(f"\n  界面三步说明要求用户：{'上下翻转（短边为轴），反面已旋转 180°' if action == 'short' else '左右反转（长边为轴）' if action == 'long' else '不翻面'}")
        got = simulate_pairing(front, back, action)
        check("照界面说明执行后，每张纸正反面配对", got == expected_pairing(n),
              describe_bad(got, expected_pairing(n)))

        n5 = 5
        sample5 = os.path.join(tmp, f"sample{n5}.pdf")
        build_sample(sample5, n5)
        result5 = drive_ui(sample5, tmp, "a1", face_down=False, rotate=True)
        front5, back5 = verify_files(f"A1 合成 {n5} 页样本（最后一张纸反面空白）",
                                     sample5, result5["front"], result5["back"],
                                     n5, face_down=False, rotate=True)
        got5 = simulate_pairing(front5, back5, prescribed_action(result5["steps"]))
        check("照界面说明执行后，每张纸正反面配对（奇数页）", got5 == expected_pairing(n5),
              describe_bad(got5, expected_pairing(n5)))

        # ---------- C 层：组合矩阵 ----------
        matrix(tmp)

        # ---------- 真实样本 ----------
        if os.path.exists(user_sample):
            real_sample(user_sample, tmp)
        else:
            print(f"\n[INFO] 找不到真实样本 {user_sample}，跳过")

    print("\n" + "=" * 72)
    if failures:
        print(f"失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        raise SystemExit(1)
    print("全部通过")


if __name__ == "__main__":
    main()

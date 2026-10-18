"""手动双面打印页序重排核心逻辑：将PDF拆分为正反面两份打印文件。

原理：单面打印机通过两次打印实现双面——
    第一次打印所有奇数页（正面），把纸张翻面放回进纸盒，
    第二次打印所有偶数页（反面），每张纸两面都有内容。

反面文件的页序由「用户怎么翻面」决定，**不是**由打印机出纸方向决定。
推导只依赖三条与机型无关的物理事实：
    1. 进纸盒从纸叠最上面逐张取纸；
    2. 出纸后最后打印的那张压在纸叠最上面（与出纸面朝上还是朝下无关）；
    3. 第二次进纸前整叠翻面会让纸叠顺序反转，不翻则保持原样。
用户翻不翻面的判据是"让空白面朝向打印面"，所以装回后必定印得上，
与进纸盒要求哪面朝上无关——这也是页序不能由出纸方向推出来的原因。

由此得到三种翻面方式（见 FLIP_NONE / FLIP_LONG_EDGE / FLIP_SHORT_EDGE）：

    不翻面        纸叠顺序不反转 → 反面文件第 1 页落在最大奇数页那张纸上，
                  故反面页序是逆序的偶数页，且奇数页文档要在最前面补一张
                  空白占位页（省掉会让后面所有页整体错一张纸）。
    左右翻/上下翻  纸叠顺序反转 → 反面文件按正序的偶数页打印即可；奇数页
                  文档末尾的空白页直接省掉。

反面页要不要旋转 180° 因机型而异（见 back_needs_rotation），需要旋转时也不设
/Rotate 标志位、不套 Form XObject，而是直接把旋转写进页面内容流，并把
位图反向转回来——原因见 _rotate_page_180 与 _counter_rotate_images 的说明。
"""

import io
import re
from pathlib import Path

import fitz

# 内容流里的绘制指令：/资源名 Do
_DO_RE = re.compile(rb"/([\w.#\-]+)\s+Do")
# 间接引用："12 0 R"
_REF_RE = re.compile(r"(\d+)\s+0\s+R")
# 资源字典条目：/名字 12 0 R
_RES_RE = re.compile(r"/([\w.#\-]+)\s+(\d+)\s+0\s+R")
# 位图兜底光栅化的分辨率与 JPEG 质量
FALLBACK_RASTER_DPI = 300
FALLBACK_RASTER_QUALITY = 90

# ------------------------------------------------------------
#  翻面方式
# ------------------------------------------------------------

#: 第二次进纸前不翻面：纸叠顺序保持原样，反面页序为逆序偶数页。
FLIP_NONE = "none"
#: 左右反转（像翻书页，以长边为轴）：整叠翻面 → 顺序反转，反面页预转 180°。
FLIP_LONG_EDGE = "long"
#: 上下翻转（纸头调转，以短边为轴）：顺序反转，反面页不旋转。
FLIP_SHORT_EDGE = "short"

FLIPS = (FLIP_NONE, FLIP_LONG_EDGE, FLIP_SHORT_EDGE)


def back_needs_rotation(flip: str) -> bool:
    """该翻面方式下，反面页是否需要旋转 180°。

    这是手动双面打印里唯一**因机器而异**的一环：纸翻面后进打印机时，纸头
    朝向是否与打印正面时一致，取决于出纸盘的前沿朝向和进纸盒要求的前沿
    朝向是否相同，两者因机型而异，没法从代码里推出来。

    本项目的取值来自实测（用户的机器：出纸与进纸的前沿方向相反）：
        左右反转（像翻书页，长边为轴）→ 反面文字是倒的，需预转 180°；
        上下翻转（纸头调转，短边为轴）→ 反面与正面同向，不旋转。

    换一台机器如果端到端试打发现反面倒了，把下面这一行改成
    `flip == FLIP_SHORT_EDGE` 即可（等价于两个选项对调）。

    界面文案与拼合预览都从这里取，别再各自判一遍——同一个规则散在多处
    最容易只改一半。
    """
    return flip == FLIP_LONG_EDGE


def page_orders(page_count: int, flip: str) -> tuple[list[int], list[int | None]]:
    """按翻面方式算出正/反两份文件的页序。

    Args:
        page_count: 源 PDF 页数。
        flip: FLIP_NONE / FLIP_LONG_EDGE / FLIP_SHORT_EDGE。

    Returns:
        (正面页序, 反面页序)，均为 0-based 源页索引；反面页序里的 None
        表示空白占位页（源文档没有这一页，但必须占一个位置）。

    Raises:
        ValueError: 翻面方式不是已知取值时抛出。
    """
    if flip not in FLIPS:
        raise ValueError(f"未知的翻面方式：{flip!r}")

    front = list(range(0, page_count, 2))
    if flip == FLIP_NONE:
        # 纸叠顺序不反转：反面文件第 1 页落在纸叠最上面那张，也就是正面文件
        # 最后打印的那张（最大奇数页）。所以从后往前取背面，末尾越界的补空白。
        back: list[int | None] = []
        for src_index in reversed(front):
            nxt = src_index + 1
            back.append(nxt if nxt < page_count else None)
        return front, back

    # 整叠翻面后顺序反转，反面文件正序打印即可对上；末尾那张空白页不必打印。
    return front, list(range(1, page_count, 2))


def simulate_sheets(page_count: int, flip: str) -> list[tuple[int, int | None]]:
    """把纸按物理流程走一遍，返回每张纸的 (正面页码, 反面页码)，均 1-based。

    None 表示该面空白（无内容可印）。列表顺序是"用户拿到手时的纸叠顺序"，
    供界面的拼合预览逐张对照。

    模拟过程就是 page_orders 的推导过程：
        ① 正面文件顺序进纸，出纸后最后打印的压在最上面 → 纸叠 = 正面页序的逆序；
        ② 翻面（FLIP_NONE 之外的两种）会把纸叠顺序再反转回来；
        ③ 按纸叠顺序再次进纸，依次印上反面文件各页。
    """
    front, back = page_orders(page_count, flip)
    stack = list(reversed(front))
    if flip != FLIP_NONE:
        stack.reverse()

    # 反面文件比纸叠短时（末尾空白页被省掉），多出来的纸叠自然配对 None
    printed = dict(zip(stack, back))
    sheets: list[tuple[int, int | None]] = []
    for src_index in front:
        face = printed.get(src_index)
        sheets.append((src_index + 1, None if face is None else face + 1))
    return sheets


class DuplexReorderer:
    """将PDF重排为手动双面打印所需的正/反面两份文件。"""

    def reorder(
        self,
        path: str,
        flip: str = FLIP_LONG_EDGE,
    ) -> tuple[bytes, bytes]:
        """按手动双面打印需求重排页面，返回 (正面bytes, 反面bytes)。

        Args:
            path: 源 PDF 文件路径。
            flip: 翻面方式，见 FLIP_NONE / FLIP_LONG_EDGE / FLIP_SHORT_EDGE。
                  它同时决定反面文件的页序和是否需要旋转 180°。

        Returns:
            (正面 PDF bytes, 反面 PDF bytes)。正面为奇数页按原顺序；
            反面按翻面方式排列，FLIP_NONE 时可能含空白占位页。
            源文档只有 1 页时反面为 b""（空批）。

        Raises:
            FileNotFoundError: 路径不存在时抛出。
            ValueError: PDF 页数为 0、或翻面方式无效时抛出。
            RuntimeError: 打开或重排过程中发生错误时抛出。
        """
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"文件不存在：{path}")

        try:
            src = fitz.open(str(file_path))
        except Exception as exc:
            raise RuntimeError(f"无法打开PDF文件：{file_path.name}\n{exc}") from exc

        try:
            n = src.page_count
            if n == 0:
                raise ValueError("PDF 文件没有页面。")

            front, back = page_orders(n, flip)
            front_bytes = self._build(src, front, rotate=False)
            back_bytes = self._build(src, back, rotate=back_needs_rotation(flip))
            return front_bytes, back_bytes
        finally:
            src.close()

    def _build(self, src: fitz.Document, pages: list[int | None], rotate: bool) -> bytes:
        """按给定页序抽取页面生成新PDF，返回 bytes。

        页序里的 None 表示空白占位页，按源文档首页尺寸插入一张白纸。

        空批（pages 为空）返回 b""——PyMuPDF 无法保存 0 页 PDF，
        由 UI 层决定是否跳过对应文件的生成。
        """
        if not pages:
            return b""
        out = fitz.open()
        try:
            for p in pages:
                if p is None:
                    first = src[0].rect
                    out.new_page(width=first.width, height=first.height)
                else:
                    out.insert_pdf(src, from_page=p, to_page=p)
            if rotate:
                for page in out:
                    _rotate_page_180(page)
                _counter_rotate_images(out)
            buffer = io.BytesIO()
            out.save(buffer)
            return buffer.getvalue()
        finally:
            out.close()


def _rotate_page_180(page: fitz.Page) -> None:
    """把整页内容旋转 180°：在最外层套一个 q ... cm ... Q。

    为什么不用 page.set_rotation()：/Rotate 只是"显示时如何处理"的属性，
    阅读器与打印驱动对它的处理并不一致，会出现"屏幕上正着、打印出来是倒的"。

    为什么不用 show_pdf_page()：它会把源页包成一层新的 Form XObject，
    页面上凭空多出 fzFrm0 -> fullpage 两层嵌套。

    直接在内容流里写变换，页面对象与源页完全一致，只多一条 cm。
    """
    doc = page.parent
    mb = page.mediabox  # 未旋转的页面框，内容流就工作在这个坐标系里
    xrefs = _page_content_xrefs(doc, page)
    content = _read_content(doc, xrefs)
    prefix = b"q\n-1 0 0 -1 %.6f %.6f cm\n" % (mb.x0 + mb.x1, mb.y0 + mb.y1)
    _write_content(doc, page.xref, xrefs, prefix + content + b"\nQ\n")


def _counter_rotate_images(doc: fitz.Document) -> None:
    """把所有位图自己转 180°，抵消掉页面层面的旋转。

    实测（粉笔导出的试卷）：页面上有大量整页位图，当位图的有效变换矩阵
    里带 180° 旋转时，部分打印机/驱动会把位图贴错——矢量文字是正的、
    位图却是反的，或者整页位图被放大成"只剩几个字"。同一批位图在正面
    文件里（矩阵是正的纯缩放平移）打印完全正常。

    所以：把像素自己转 180°，再在绘制处套一个 [-1 0 0 -1 1 1]，
    位图最终的有效矩阵就回到"纯缩放 + 平移"，与能正常打印的正面一致。
    视觉效果完全不变。
    """
    plans = _image_draws(doc)
    if not plans:
        return

    rotated: set[int] = set()
    failed = False
    for mapping in plans.values():
        for target in mapping.values():
            if target in rotated:
                continue
            if _rotate_image_180(doc, target):
                rotated.add(target)
            else:
                failed = True

    if failed:
        # 兜底：有位图没法安全地转（蒙版/1位图等），整个反面改为
        # 逐页光栅化输出。宁可文件大一点、字略糊，也不能打印出错。
        _rasterize_pages(doc)
        return

    for cxref, mapping in plans.items():
        data = doc.xref_stream(cxref)
        for name, target in mapping.items():
            if target not in rotated:
                continue
            data = _DO_RE.sub(
                lambda m, n=name: b"q -1 0 0 -1 1 1 cm /" + m.group(1) + b" Do\nQ"
                if m.group(1) == n else m.group(0),
                data,
            )
        doc.update_stream(cxref, data)


def _image_draws(doc: fitz.Document) -> dict[int, dict[bytes, int]]:
    """找出内容流里绘制到的位图：{内容流xref: {资源名: 位图xref}}。"""
    plans: dict[int, dict[bytes, int]] = {}
    for owner_xref, cxrefs in _iter_containers(doc):
        xobjects = _resource_map(doc, owner_xref, "XObject")
        if not xobjects:
            continue
        for cxref in cxrefs:
            data = doc.xref_stream(cxref)
            for name in set(_DO_RE.findall(data)):
                target = xobjects.get(name.decode("latin-1"))
                if target is not None and _key(doc, target, "Subtype") == "/Image":
                    plans.setdefault(cxref, {})[name] = target
    return plans


def _rotate_image_180(doc: fitz.Document, xref: int) -> bool:
    """把位图对象的像素旋转 180°（连同它的 /SMask），失败返回 False。"""
    if _key(doc, xref, "ImageMask") == "true":
        return False
    if _key(doc, xref, "BitsPerComponent") not in (None, "8"):
        return False

    from PIL import Image

    try:
        pix = fitz.Pixmap(doc, xref)
    except Exception:  # noqa: BLE001 - 解不开的位图交给兜底方案
        return False

    color_mode = {1: "L", 3: "RGB", 4: "CMYK"}.get(pix.n)
    if color_mode is None or pix.alpha or pix.colorspace is None:
        return False

    smask = _key(doc, xref, "SMask")
    if smask:
        smask_xref = int(smask.split()[0]) if _REF_RE.search(smask) else None
        # 蒙版和主图同尺寸同朝向，必须一起转，否则透明度会错位
        if smask_xref is None or not _rotate_image_180(doc, smask_xref):
            return False

    try:
        image = Image.frombytes(color_mode, (pix.width, pix.height), pix.samples)
        data = image.transpose(Image.Transpose.ROTATE_180).tobytes()
    except Exception:  # noqa: BLE001
        return False

    doc.update_stream(xref, data, compress=True)
    doc.xref_set_key(xref, "DecodeParms", "null")
    return True


def _rasterize_pages(doc: fitz.Document) -> None:
    """兜底方案：把每一页渲染成一张位图后用最朴素的方式画出。

    此时页面内容就是"一张图 + 正的缩放平移矩阵"，不需要打印机理解任何
    旋转，是最不容易出错的形式。

    用 JPEG（/DCTDecode）而不是 Flate：渲染出来的抗锯齿文字用 Flate 压
    不动（一页要 26MB），JPEG 只要几百 KB；而且打印机对 JPEG 的支持比
    Flate 更普遍。自己写流是为了不依赖 Document.save 的压缩选项。
    """
    from PIL import Image

    for page in doc:
        pix = page.get_pixmap(dpi=FALLBACK_RASTER_DPI, alpha=False, colorspace=fitz.csRGB)
        buffer = io.BytesIO()
        Image.frombytes("RGB", (pix.width, pix.height), pix.samples).save(
            buffer, "JPEG", quality=FALLBACK_RASTER_QUALITY, optimize=True
        )

        xref = doc.get_new_xref()
        doc.update_object(
            xref,
            "<< /Type /XObject /Subtype /Image"
            f" /Width {pix.width} /Height {pix.height}"
            " /ColorSpace /DeviceRGB /BitsPerComponent 8 >>",
        )
        # update_stream(compress=False) 会把 /Filter 清掉，所以写完流再设回去
        doc.update_stream(xref, buffer.getvalue(), compress=False)
        doc.xref_set_key(xref, "Filter", "/DCTDecode")

        rect = page.rect
        draw = (
            "q %.4f 0 0 %.4f %.4f %.4f cm /FzRaster Do\nQ\n"
            % (rect.width, rect.height, rect.x0, rect.y0)
        ).encode()
        _write_content(doc, page.xref, _page_content_xrefs(doc, page), draw)
        # 原来的资源已经没人引用了，换成只指向这张图，免得文件里留一堆死对象
        doc.xref_set_key(
            page.xref,
            "Resources",
            f"<< /ProcSet [/PDF /ImageC] /XObject << /FzRaster {xref} 0 R >> >>",
        )


def _key(doc: fitz.Document, xref: int, key: str) -> str | None:
    """读对象字典里的一个键，不存在时返回 None。"""
    kind, value = doc.xref_get_key(xref, key)
    return None if kind == "null" else value


def _resource_map(doc: fitz.Document, owner_xref: int, category: str) -> dict[str, int]:
    """读页面/Form 的资源字典：{资源名: 对象xref}。"""
    value = _key(doc, owner_xref, f"Resources/{category}")
    if not value:
        return {}
    return {name: int(xref) for name, xref in _RES_RE.findall(value)}


def _iter_containers(doc: fitz.Document) -> list[tuple[int, list[int]]]:
    """列出文档里所有能绘制内容的对象：(所有者xref, 内容流xref列表)。

    页面和 Form XObject 都是容器——位图可能直接画在页面上，也可能画在
    页面引用的 Form 里。
    """
    containers: list[tuple[int, list[int]]] = []
    for page in doc:
        containers.append((page.xref, _page_content_xrefs(doc, page)))
    for xref in range(1, doc.xref_length()):
        if _key(doc, xref, "Subtype") == "/Form":
            containers.append((xref, [xref]))
    return containers


def _page_content_xrefs(doc: fitz.Document, page: fitz.Page) -> list[int]:
    """页面的 /Contents 可能是单个引用，也可能是引用数组。"""
    value = _key(doc, page.xref, "Contents")
    if not value:
        return []
    return [int(m) for m in _REF_RE.findall(value)]


def _read_content(doc: fitz.Document, xrefs: list[int]) -> bytes:
    """把页面的多个内容流拼成一条（PyMuPDF 保存时会重新压缩）。"""
    parts = []
    for xref in xrefs:
        try:
            parts.append(doc.xref_stream(xref))
        except Exception:  # noqa: BLE001 - 非流对象直接跳过
            continue
    return b"\n".join(parts)


def _write_content(
    doc: fitz.Document, owner_xref: int, xrefs: list[int], data: bytes
) -> None:
    """把内容写回页面，多余的内容流合并到第一条上。"""
    if not xrefs:
        xref = doc.get_new_xref()
        doc.update_object(xref, "<<>>")
        doc.update_stream(xref, data)
        doc.xref_set_key(owner_xref, "Contents", f"{xref} 0 R")
        return
    try:
        doc.update_stream(xrefs[0], data)
    except Exception:  # noqa: BLE001 - 不可写时另起一条流
        xref = doc.get_new_xref()
        doc.update_object(xref, "<<>>")
        doc.update_stream(xref, data)
        xrefs = [xref]
    if len(xrefs) > 1:
        doc.xref_set_key(owner_xref, "Contents", f"[{xrefs[0]} 0 R]")

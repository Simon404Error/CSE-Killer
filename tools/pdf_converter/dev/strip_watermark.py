# -*- coding: utf-8 -*-
"""去除「申论实战突破50题.pdf」中的 Fenbi.com 水印。

原理:InDesign 导出时把水印画在页面内容流末尾,由
    /Artifact <</Subtype /Watermark /Type /Pagination>>BDC ... EMC
标记包裹,内部是 /GS3(25% 透明度) + /Fm0(304 个转曲线的字形)绘制。
只要整块删除这个 Artifact 段落,水印即消失,正文(独立于 Fm0)不受影响。

用法:
    python -I dev/strip_watermark.py <src> <dst> [--dry-run]
    python -I dev/strip_watermark.py <src> <dst> --verify-pages 1,5,100
"""
import re
import sys
from pathlib import Path

import fitz

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WM_PAT = re.compile(
    rb"/Artifact\s*<<[^<>]*?/Subtype\s*/Watermark[^<>]*?>>\s*BDC\b.*?\bEMC\b\s*",
    re.DOTALL,
)


def strip(src: str, dst: str, dry_run: bool = False) -> dict:
    doc = fitz.open(src)
    stats = {"pages": doc.page_count, "blocks_removed": 0, "bytes_removed": 0, "missed": []}

    for pno in range(doc.page_count):
        page = doc[pno]
        page_hits = 0
        for xref in page.get_contents():
            data = doc.xref_stream(xref)
            if not data:
                continue
            new, n = WM_PAT.subn(b"", data)
            if n:
                page_hits += n
                stats["blocks_removed"] += n
                stats["bytes_removed"] += len(data) - len(new)
                if not dry_run:
                    doc.update_stream(xref, new)
        if page_hits == 0:
            stats["missed"].append(pno + 1)

    if not dry_run:
        # 水印 OCG 已无内容引用,从可选内容配置中移除(否则阅读器图层面板残留空图层)
        cat = doc.pdf_catalog()
        ocp = doc.xref_get_key(cat, "OCProperties")
        if ocp[1] and "836 0 R" in ocp[1]:
            doc.xref_set_key(cat, "OCProperties", "null")
        doc.save(dst, garbage=3, deflate=True)
    doc.close()
    return stats


def main() -> None:
    src, dst = sys.argv[1], sys.argv[2]
    dry = "--dry-run" in sys.argv
    verify = None
    if "--verify-pages" in sys.argv:
        verify = [int(x) for x in sys.argv[sys.argv.index("--verify-pages") + 1].split(",")]

    stats = strip(src, dst, dry_run=dry)
    print(f"页数: {stats['pages']}")
    print(f"移除水印块: {stats['blocks_removed']}  释放流字节: {stats['bytes_removed']}")
    print(f"未命中的页: {stats['missed'] or '无'}")

    if dry:
        return
    print(f"\n已输出: {dst}  ({Path(dst).stat().st_size/1024/1024:.1f} MB)")
    print(f"源文件: {Path(src).stat().st_size/1024/1024:.1f} MB")

    # 渲染验证图
    outdir = Path(__file__).resolve().parent / "out_wm"
    outdir.mkdir(exist_ok=True)
    doc = fitz.open(dst)
    for p in verify or [1, 5, 100, 168]:
        if 1 <= p <= doc.page_count:
            path = outdir / f"clean_p{p:03d}.png"
            doc[p - 1].get_pixmap(dpi=110).save(path)
            print(f"已渲染: {path}")
    doc.close()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3.11
# Status: experimental
# Path: cli — python3.11 scripts/pdf_marker_batch.py {convert, batch, scan} [args]
"""pdf_marker_batch — Background high-quality PDF conversion using marker-pdf.

Uses marker-pdf (Surya layout model) for superior structure extraction.
NOT for realtime use — model load is heavy, conversion is slow on CPU.
Designed for background/batch processing (tmux, systemd, cron).

Usage:
  python3.11 pdf_marker_batch.py convert <pdf_path> [output_dir]
  python3.11 pdf_marker_batch.py batch <pdf_dir> [output_dir]
  python3.11 pdf_marker_batch.py scan <pdf_dir>           # list PDFs with type detection
  python3.11 pdf_marker_batch.py info <pdf_path>          # show PDF metadata only (no marker)
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

# ── PyMuPDF (lightweight, no model load) ──

def _check_pdf_type(pdf_path: str) -> tuple[str, int, float]:
    """Return (pdf_type, num_pages, avg_chars_per_page)."""
    import fitz
    doc = fitz.open(pdf_path)
    n = len(doc)
    check = min(5, n)
    total = 0
    for i in range(check):
        total += len(doc[i].get_text().strip())
    doc.close()
    avg = total / check if check > 0 else 0
    ptype = "digital" if avg >= 20 else "scanned"
    return ptype, n, avg


def _get_metadata(pdf_path: str) -> dict:
    """Lightweight metadata extraction (no marker model)."""
    import fitz
    doc = fitz.open(pdf_path)
    n_pages = doc.page_count
    info = doc.metadata or {}
    toc = doc.get_toc()[:50]
    doc.close()
    return {
        "file": os.path.basename(pdf_path),
        "pages": n_pages,
        "title": info.get("title", ""),
        "author": info.get("author", ""),
        "subject": info.get("subject", ""),
        "created": info.get("creationDate", ""),
        "modified": info.get("modDate", ""),
        "toc_count": len(toc),
    }


# ── marker-pdf (heavy) ──

_converter = None


def _get_converter(page_range: str = ""):
    """Lazy-load marker-pdf converter (once)."""
    global _converter
    if _converter is not None and not page_range:
        return _converter
    from marker.converters.pdf import PdfConverter
    from marker.models import create_model_dict
    from marker.config.parser import ConfigParser

    config = {}
    if page_range:
        config["page_range"] = page_range
    config["output_format"] = "markdown"

    if page_range:
        # With page_range, create a new converter (not cached)
        cp = ConfigParser(config)
        return PdfConverter(
            artifact_dict=create_model_dict(device="cpu"),
            config=cp.generate_config_dict(),
            processor_list=cp.get_processors(),
            renderer=cp.get_renderer(),
            llm_service=cp.get_llm_service(),
        )

    # Cached default (all pages, no config)
    if _converter is not None:
        return _converter
    _converter = PdfConverter(artifact_dict=create_model_dict(device="cpu"))
    return _converter


def convert_pdf(pdf_path: str, output_dir: str = "", pages: int = 0) -> dict:
    """Convert a single PDF to markdown+json using marker-pdf.

    Returns result dict with keys: file, pdf_type, pages, output_md, output_json, elapsed_s.
    """
    from marker.output import text_from_rendered

    ptype, n_pages, avg_chars = _check_pdf_type(pdf_path)

    page_range = f"1-{pages}" if pages > 0 else ""
    converter = _get_converter(page_range)
    fname = Path(pdf_path).stem

    t0 = time.time()
    rendered = converter(pdf_path)
    text, raw_meta, images = text_from_rendered(rendered)
    elapsed = time.time() - t0

    # raw_meta can be dict or str depending on marker version
    if isinstance(raw_meta, dict):
        converted_pages = raw_meta.get("pages", pages or n_pages)
    else:
        converted_pages = pages or n_pages

    md = f"# {fname}\n\n"
    md += f"> marker-pdf 변환 | {ptype.upper()} | {converted_pages}페이지 | {elapsed:.0f}초\n\n"
    md += text

    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        md_path = os.path.join(output_dir, f"{fname}.md")
        json_path = os.path.join(output_dir, f"{fname}.json")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md)
        result = {
            "metadata": {
                "file": os.path.basename(pdf_path),
                "pdf_type": ptype,
                "pages": n_pages,
                "converted_pages": converted_pages,
                "avg_chars_per_page": round(avg_chars, 1),
                "elapsed_s": round(elapsed, 1),
                "s_per_page": round(elapsed / converted_pages, 1),
                "output_md": md_path,
                "output_json": json_path,
            },
            "content": text,
        }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    else:
        result = {
            "metadata": {
                "file": os.path.basename(pdf_path),
                "pdf_type": ptype,
                "pages": n_pages,
                "elapsed_s": round(elapsed, 1),
            },
            "content": text,
        }

    return result


# ── CLI commands ──

def cmd_convert(args):
    pdf_path = os.path.abspath(args.pdf)
    if not os.path.isfile(pdf_path):
        print(f"[error] File not found: {pdf_path}")
        sys.exit(1)
    out = os.path.abspath(args.output_dir) if args.output_dir else ""
    print(f"[*] Converting: {pdf_path}")
    ptype, n, _ = _check_pdf_type(pdf_path)
    print(f"[*] Type: {ptype.upper()} | Pages: {n}")
    if args.pages:
        print(f"[*] Processing first {args.pages} pages (--pages)")
    if n > args.max_pages and not args.pages:
        print(f"[!] Skipping: {n} pages exceeds --max-pages {args.max_pages}")
        sys.exit(1)
    print(f"[*] Loading marker-pdf model (first load: ~3.3GB model cache)...")
    t0 = time.time()
    result = convert_pdf(pdf_path, out, pages=args.pages)
    print(f"[+] Done in {result['metadata']['elapsed_s']:.1f}s")
    if out:
        print(f"[+] Output: {result['metadata']['output_md']}")
    else:
        print(f"[+] Content ({len(result['content'])} chars):")
        print(result["content"][:1000])
    if args.pages:
        print(f"[!] Partial conversion ({args.pages}p of {n}p). Run without --pages for full.")


def cmd_batch(args):
    pdf_dir = os.path.abspath(args.pdf_dir)
    if not os.path.isdir(pdf_dir):
        print(f"[error] Directory not found: {pdf_dir}")
        sys.exit(1)
    out_base = os.path.abspath(args.output_dir) if args.output_dir else os.path.join(pdf_dir, "_marker_output")
    os.makedirs(out_base, exist_ok=True)

    pdfs = sorted(f for f in os.listdir(pdf_dir) if f.lower().endswith(".pdf"))
    if not pdfs:
        print(f"[!] No PDFs found in {pdf_dir}")
        return

    print(f"[*] Found {len(pdfs)} PDFs in {pdf_dir}")
    print(f"[*] Loading marker-pdf model...")
    _get_converter()

    for i, fname in enumerate(pdfs, 1):
        pdf_path = os.path.join(pdf_dir, fname)
        ptype, n, _ = _check_pdf_type(pdf_path)
        print(f"  [{i}/{len(pdfs)}] {fname} ({ptype}, {n}p)")
        if n > args.max_pages:
            print(f"    [!] Skipped ({n} > {args.max_pages} pages)")
            continue
        try:
            odir = os.path.join(out_base, Path(fname).stem)
            result = convert_pdf(pdf_path, odir, pages=args.pages)
            print(f"    [+] {result['metadata']['elapsed_s']:.1f}s")
        except Exception as e:
            print(f"    [error] {e}")


def cmd_scan(args):
    pdf_dir = os.path.abspath(args.pdf_dir)
    if not os.path.isdir(pdf_dir):
        print(f"[error] Directory not found: {pdf_dir}")
        sys.exit(1)
    pdfs = sorted(f for f in os.listdir(pdf_dir) if f.lower().endswith(".pdf"))
    if not pdfs:
        print(f"[!] No PDFs found")
        return
    for f in pdfs:
        fp = os.path.join(pdf_dir, f)
        ptype, n, avg = _check_pdf_type(fp)
        size_mb = os.path.getsize(fp) / 1024 / 1024
        print(f"  {ptype:8s}  {n:3d}p  {avg:6.1f}c  {size_mb:5.1f}MB  {f}")


def cmd_info(args):
    pdf_path = os.path.abspath(args.pdf)
    if not os.path.isfile(pdf_path):
        print(f"[error] File not found: {pdf_path}")
        sys.exit(1)
    ptype, n, avg = _check_pdf_type(pdf_path)
    meta = _get_metadata(pdf_path)
    meta["pdf_type"] = ptype
    meta["avg_chars_per_page"] = round(avg, 1)
    size_mb = os.path.getsize(pdf_path) / 1024 / 1024
    meta["size_mb"] = round(size_mb, 1)
    print(json.dumps(meta, ensure_ascii=False, indent=2))


# ── main ──

def main():
    parser = argparse.ArgumentParser(description="pdf_marker_batch — Background PDF conversion with marker-pdf")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("convert", help="Convert a single PDF")
    p.add_argument("pdf", help="Path to PDF file")
    p.add_argument("output_dir", nargs="?", default="", help="Output directory (optional, stdout if omitted)")
    p.add_argument("--pages", type=int, default=0, help="Only convert first N pages (for quick preview)")
    p.add_argument("--max-pages", type=int, default=200, help="Skip PDFs over this page count")

    p = sub.add_parser("batch", help="Convert all PDFs in a directory")
    p.add_argument("pdf_dir", help="Directory with PDF files")
    p.add_argument("output_dir", nargs="?", default="", help="Base output directory")
    p.add_argument("--pages", type=int, default=0, help="Only convert first N pages per PDF")
    p.add_argument("--max-pages", type=int, default=200, help="Skip PDFs over this page count")

    p = sub.add_parser("scan", help="Scan directory and detect PDF types (no marker model)")
    p.add_argument("pdf_dir", help="Directory with PDF files")

    p = sub.add_parser("info", help="Show PDF metadata (no marker model)")
    p.add_argument("pdf", help="Path to PDF file")

    args = parser.parse_args()

    {
        "convert": cmd_convert,
        "batch": cmd_batch,
        "scan": cmd_scan,
        "info": cmd_info,
    }[args.command](args)


if __name__ == "__main__":
    main()

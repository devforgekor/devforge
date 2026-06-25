#!/usr/bin/env python3.11
# Status: experimental
# Path: MCP client (mcp.json — ~/.claude/mcp.json)
"""pdf_reader_mcp — PDF extraction MCP server using PyMuPDF.

Provides tools for reading, analyzing, and extracting PDF content.
Designed for both quick reading and structured JSON export for embedding.

Register in mcp.json:
  "pdf-reader": {
    "type": "stdio",
    "command": "python3.11",
    "args": ["/opt/projects/server/scripts/pdf_reader_mcp.py"]
  }
"""

import json, os, sys

import fitz  # PyMuPDF


def _read_message():
    headers = {}
    while True:
        line = sys.stdin.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        if ":" in line:
            key, val = line.split(":", 1)
            headers[key.strip().lower()] = val.strip()
    length = int(headers.get("content-length", 0))
    if length == 0:
        return None
    return json.loads(sys.stdin.read(length))


def _send_message(msg):
    body = json.dumps(msg, ensure_ascii=False)
    payload = f"Content-Length: {len(body)}\r\n\r\n{body}"
    sys.stdout.write(payload)
    sys.stdout.flush()


def _safe_path(path):
    allowed = ["/opt/ai_data", "/opt/projects/server", "/tmp"]
    full = os.path.realpath(os.path.normpath(path))
    if not any(full.startswith(os.path.realpath(a) + "/") or full == os.path.realpath(a) for a in allowed):
        raise ValueError(f"Path must be under {', '.join(allowed)}")
    if not os.path.isfile(full):
        raise FileNotFoundError(f"File not found: {full}")
    return full


def _analyze_pdf(path):
    doc = fitz.open(path)
    info = doc.metadata or {}
    toc = doc.get_toc()[:50]
    return {
        "file": os.path.basename(path),
        "pages": doc.page_count,
        "title": info.get("title", ""),
        "author": info.get("author", ""),
        "subject": info.get("subject", ""),
        "format": info.get("format", ""),
        "created": info.get("creationDate", ""),
        "modified": info.get("modDate", ""),
        "toc": [{"level": l, "title": t, "page": p} for l, t, p in toc],
    }


def _page_blocks(doc, page_num):
    page = doc[page_num]
    blocks = page.get_text("dict")["blocks"]
    result = []
    for b in blocks:
        if b["type"] == 0:  # text
            lines = []
            for l in b.get("lines", []):
                spans = [s["text"] for s in l.get("spans", [])]
                lines.append("".join(spans))
            result.append({"type": "text", "text": "\n".join(lines)})
        elif b["type"] == 1:  # image
            result.append({"type": "image", "width": b.get("width", 0), "height": b.get("height", 0)})
    return result


def _extract_pdf_text(path, page_from=0, page_to=None, structured=False):
    doc = fitz.open(path)
    page_to = page_to if page_to is not None else doc.page_count - 1
    page_to = min(page_to, doc.page_count - 1)

    if structured:
        pages = []
        for i in range(page_from, page_to + 1):
            page = doc[i]
            text = page.get_text()
            blocks = _page_blocks(doc, i)
            pages.append({
                "page": i + 1,
                "text": text.strip(),
                "blocks": blocks,
                "images": sum(1 for b in blocks if b["type"] == "image"),
            })
        return {"total_pages": doc.page_count, "pages": pages}
    else:
        texts = []
        for i in range(page_from, page_to + 1):
            text = doc[i].get_text().strip()
            if text:
                texts.append(f"--- Page {i+1} ---\n{text}")
        return "\n\n".join(texts) or "(empty)"


def _handle_call(name, args):
    path = args.get("path", "")

    if name == "analyze_pdf":
        try:
            full = _safe_path(path)
        except (ValueError, FileNotFoundError) as e:
            return json.dumps({"error": str(e)})
        doc = fitz.open(full)
        result = _analyze_pdf(full)
        doc.close()
        return json.dumps(result, ensure_ascii=False)

    elif name == "extract_text":
        try:
            full = _safe_path(path)
        except (ValueError, FileNotFoundError) as e:
            return json.dumps({"error": str(e)})
        page_from = args.get("page_from", 0)
        page_to = args.get("page_to")
        doc = fitz.open(full)
        result = _extract_pdf_text(full, page_from=page_from, page_to=page_to)
        doc.close()
        return result

    elif name == "extract_page":
        try:
            full = _safe_path(path)
        except (ValueError, FileNotFoundError) as e:
            return json.dumps({"error": str(e)})
        page_num = int(args.get("page", 1))
        doc = fitz.open(full)
        if page_num < 1 or page_num > doc.page_count:
            doc.close()
            return json.dumps({"error": f"Page {page_num} out of range (1-{doc.page_count})"})
        text = doc[page_num - 1].get_text().strip()
        doc.close()
        return text or "(empty page)"

    elif name == "pdf_to_json":
        try:
            full = _safe_path(path)
        except (ValueError, FileNotFoundError) as e:
            return json.dumps({"error": str(e)})
        doc = fitz.open(full)
        info = _analyze_pdf(full)
        pages_data = _extract_pdf_text(full, structured=True)
        doc.close()
        result = {**info, **pages_data}
        return json.dumps(result, ensure_ascii=False, indent=2)

    elif name == "extract_images":
        try:
            full = _safe_path(path)
        except (ValueError, FileNotFoundError) as e:
            return json.dumps({"error": str(e)})
        output_dir = args.get("output_dir", "/tmp/pdf_images")
        doc = fitz.open(full)
        extracted = []
        os.makedirs(output_dir, exist_ok=True)
        for page_num in range(doc.page_count):
            for img_idx, img in enumerate(doc.get_page_images(page_num)):
                xref = img[0]
                pix = fitz.Pixmap(doc, xref)
                ext = "png" if pix.n < 5 else "jpg"
                if ext == "jpg":
                    # CMYK → RGB
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                fname = f"page{page_num+1}_img{img_idx}.{ext}"
                fpath = os.path.join(output_dir, fname)
                pix.save(fpath)
                extracted.append(fpath)
                pix = None
        doc.close()
        return json.dumps({"extracted": len(extracted), "output_dir": output_dir, "files": extracted}, ensure_ascii=False)

    elif name == "list_pdfs":
        base = args.get("directory", "/opt/ai_data/pdf")
        try:
            full = os.path.realpath(base)
        except Exception:
            return json.dumps({"error": f"Invalid directory: {base}"})
        if not os.path.isdir(full):
            return json.dumps({"error": f"Directory not found: {full}"})
        pdfs = []
        for f in sorted(os.listdir(full)):
            if f.lower().endswith(".pdf"):
                fpath = os.path.join(full, f)
                size = os.path.getsize(fpath)
                pdfs.append({"file": f, "size_bytes": size, "path": fpath})
        return json.dumps({"directory": full, "count": len(pdfs), "files": pdfs}, ensure_ascii=False)

    return json.dumps({"error": f"Unknown tool: {name}"})


_TOOLS = [
    {
        "name": "analyze_pdf",
        "description": "Get PDF metadata and structure (title, author, pages, TOC).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to PDF file"}
            },
            "required": ["path"],
        },
    },
    {
        "name": "extract_text",
        "description": "Extract all text from a PDF as plain text, page by page.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to PDF file"},
                "page_from": {"type": "integer", "description": "Start page (0-based, default 0)", "default": 0},
                "page_to": {"type": "integer", "description": "End page (0-based, default last)"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "extract_page",
        "description": "Extract text from a single page by page number (1-based).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to PDF file"},
                "page": {"type": "integer", "description": "Page number (1-based)", "default": 1},
            },
            "required": ["path"],
        },
    },
    {
        "name": "pdf_to_json",
        "description": "Full structured extraction: metadata + per-page text + block layout. Ready for JSON storage or embedding.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to PDF file"}
            },
            "required": ["path"],
        },
    },
    {
        "name": "extract_images",
        "description": "Extract all embedded images from a PDF to a directory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to PDF file"},
                "output_dir": {"type": "string", "description": "Output directory (default /tmp/pdf_images)"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_pdfs",
        "description": "List all PDF files in a directory with file sizes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "directory": {"type": "string", "description": "Directory to scan (default /opt/ai_data/pdf)", "default": "/opt/ai_data/pdf"}
            },
            "required": [],
        },
    },
]


def _not_impl(msg, req_id):
    _send_message({
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {msg.get('method', '?')}"},
    })


def main():
    while True:
        msg = _read_message()
        if msg is None:
            break

        method = msg.get("method", "")
        req_id = msg.get("id")

        if req_id is None:
            continue

        params = msg.get("params", {})

        if method == "initialize":
            _send_message({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "pdf-reader-mcp", "version": "1.0.0"},
                },
            })
        elif method == "tools/list":
            _send_message({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": _TOOLS},
            })
        elif method == "tools/call":
            name = params.get("name", "")
            arguments = params.get("arguments", {})
            result = _handle_call(name, arguments)
            _send_message({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": result}]},
            })
        elif method == "shutdown":
            _send_message({"jsonrpc": "2.0", "id": req_id, "result": None})
            break
        else:
            _not_impl(msg, req_id)


if __name__ == "__main__":
    main()

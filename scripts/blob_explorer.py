#!/usr/bin/env python3
# Status: production
# Path: Caddy reverse-proxy
"""blob_explorer.py --- Azure Blob web interface: send + receive.

/send     — upload files to Blob (user → system)
/receive  — browse & download from Blob (system → user)

Usage:
  python3 blob_explorer.py              # listen on 127.0.0.1:8085
"""

import cgi
import html
import json
import os
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions

# ── Config ────────────────────────────────────────────────────────────────────
ACCOUNT_NAME = "stshareddevforgeprodkrc"
CONTAINER = "devforge"
LISTEN_ADDR = os.environ.get("BLOB_EXPLORER_LISTEN", "127.0.0.1:8085")
SAS_HOURS = int(os.environ.get("BLOB_EXPLORER_SAS_HOURS", "1"))
UPLOAD_PREFIX = "uploads/"

_account_key: Optional[str] = None


def _get_account_key() -> str:
    global _account_key
    if _account_key:
        return _account_key
    sf = Path.home() / ".config/devforge/secrets.env"
    with open(sf) as f:
        for line in f:
            if line.startswith("AZURE_STORAGE_ACCOUNT_KEY="):
                _account_key = line.strip().split("=", 1)[1].strip("'\"")
                break
    if not _account_key:
        raise RuntimeError("AZURE_STORAGE_ACCOUNT_KEY not found")
    return _account_key


def _blob_service():
    return BlobServiceClient(
        account_url=f"https://{ACCOUNT_NAME}.blob.core.windows.net",
        credential=_get_account_key(),
    )


def _list_blobs(prefix: str) -> list[dict]:
    svc = _blob_service()
    cc = svc.get_container_client(CONTAINER)
    results = []
    for blob in cc.list_blobs(name_starts_with=prefix):
        if blob.name == prefix:
            continue
        results.append({
            "name": blob.name,
            "size": blob.size or 0,
            "updated": blob.last_modified.isoformat() if blob.last_modified else "",
        })
    results.sort(key=lambda b: (0 if "/" in b["name"][len(prefix):] else 1, b["name"]))
    return results


def _virtual_tree(prefix: str, blobs: list[dict]) -> dict:
    dirs: dict[str, dict] = {}
    files = []
    strip = len(prefix)
    for b in blobs:
        rel = b["name"][strip:]
        if "/" in rel:
            dname = rel.split("/")[0]
            if dname:
                dirs.setdefault(dname, {"name": dname, "count": 0, "total_size": 0})
                dirs[dname]["count"] += 1
                dirs[dname]["total_size"] += b["size"]
        else:
            files.append({"name": rel, "size": b["size"], "updated": b.get("updated", "")})
    return {"dirs": sorted(dirs.values(), key=lambda d: d["name"]), "files": files}


def _generate_sas(blob_name: str) -> str:
    key = _get_account_key()
    sas = generate_blob_sas(
        account_name=ACCOUNT_NAME, container_name=CONTAINER, blob_name=blob_name,
        account_key=key, permission=BlobSasPermissions(read=True),
        expiry=datetime.now(timezone.utc) + timedelta(hours=SAS_HOURS),
    )
    return f"https://{ACCOUNT_NAME}.blob.core.windows.net/{CONTAINER}/{blob_name}?{sas}"


def _upload_blob(filename: str, data: bytes) -> str:
    svc = _blob_service()
    cc = svc.get_container_client(CONTAINER)
    utc_ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    blob_name = f"{UPLOAD_PREFIX}{utc_ts}_{filename}"
    cc.upload_blob(blob_name, data, overwrite=True)
    return blob_name


def _size_fmt(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    else:
        return f"{size / (1024 * 1024):.1f} MB"


# ── HTML ──────────────────────────────────────────────────────────────────────
STYLE = """<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, system-ui, sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; max-width: 900px; margin: 0 auto; }
  h1 { color: #58a6ff; font-size: 1.3em; margin-bottom: 16px; }
  h1 span { color: #8b949e; font-size: 0.7em; }
  .nav { display: flex; gap: 12px; margin-bottom: 20px; }
  .nav a { color: #8b949e; text-decoration: none; padding: 6px 14px; border-radius: 6px; border: 1px solid #30363d; font-size: 0.9em; }
  .nav a.active { color: #58a6ff; border-color: #58a6ff; }
  .breadcrumb { margin-bottom: 20px; font-size: 0.9em; color: #8b949e; }
  .breadcrumb a { color: #58a6ff; text-decoration: none; }
  .breadcrumb a:hover { text-decoration: underline; }
  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; padding: 8px 12px; border-bottom: 1px solid #30363d; color: #8b949e; font-weight: 600; font-size: 0.85em; }
  td { padding: 8px 12px; border-bottom: 1px solid #21262d; font-size: 0.9em; }
  tr:hover { background: #161b22; }
  .dir a { color: #58a6ff; text-decoration: none; font-weight: 500; }
  .dir a:hover { text-decoration: underline; }
  .file a { color: #7ee787; text-decoration: none; }
  .file a:hover { text-decoration: underline; }
  .size { color: #8b949e; text-align: right; white-space: nowrap; }
  .empty { text-align: center; color: #8b949e; padding: 40px; }
  .upload-area { border: 2px dashed #30363d; border-radius: 8px; padding: 40px; text-align: center; margin: 20px 0; transition: border-color .2s, background .2s; }
  .upload-area.drag-over { border-color: #58a6ff; background: rgba(88,166,255,0.06); }
  .upload-area input[type=file] { display: none; }
  .upload-area label { cursor: pointer; color: #58a6ff; font-size: 1em; display: inline-block; }
  .upload-area .hint { color: #8b949e; font-size: 0.85em; margin-top: 8px; }
  .upload-area .drop-hint { display: none; color: #58a6ff; font-size: 0.9em; margin-top: 4px; }
  .upload-area.drag-over .drop-hint { display: block; }
  .upload-area.drag-over .select-hint { display: none; }
  .file-types { color: #484f58; font-size: 0.78em; margin-top: 12px; padding-top: 12px; border-top: 1px solid #21262d; }
  .file-types b { color: #8b949e; }
  .btn { background: #238636; color: #fff; border: none; padding: 8px 20px; border-radius: 6px; cursor: pointer; font-size: 0.9em; margin-top: 12px; }
  .btn:hover { background: #2ea043; }
  .btn:disabled { background: #30363d; cursor: not-allowed; }
  .uploaded-name { color: #7ee787; font-size: 0.9em; margin-top: 8px; display: block; }
  .name-error { color: #f85149; font-size: 0.85em; margin-top: 4px; display: none; }
  .result { background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 16px; margin-top: 20px; }
  .result .ok { color: #7ee787; }
  .result .err { color: #f85149; }
  .result code { display: block; margin-top: 8px; word-break: break-all; color: #c9d1d9; font-size: 0.85em; }
  .footer { margin-top: 24px; font-size: 0.8em; color: #484f58; text-align: center; }
  .count { color: #8b949e; font-size: 0.85em; font-weight: normal; }
</style>"""

PAGE_TOP = """<!DOCTYPE html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>DevForge Blob</title>{style}</head><body>
<h1>📦 DevForge Blob <span>{subtitle}</span></h1>
<div class="nav"><a href="/send"{send_active}>📤 보내기</a><a href="/receive"{recv_active}>📥 받기</a></div>
"""

PAGE_BOTTOM = '<div class="footer">{footer}</div></body></html>'


def _page(subtitle: str, body: str, mode: str, footer: str = "") -> str:
    return PAGE_TOP.format(
        style=STYLE, subtitle=subtitle,
        send_active=' class="active"' if mode == "send" else "",
        recv_active=' class="active"' if mode == "receive" else "",
    ) + body + PAGE_BOTTOM.format(footer=footer)


def _breadcrumb(path: str) -> str:
    if not path:
        return '<div class="breadcrumb"><a href="/receive">🏠 root</a></div>'
    parts = [p for p in path.split("/") if p]
    crumbs = [('🏠 root', '/receive')]
    current = "/receive"
    for p in parts:
        current += "/" + p
        crumbs.append((p, current))
    html_parts = []
    for i, (label, link) in enumerate(crumbs):
        if i == len(crumbs) - 1:
            html_parts.append(f'<span>{html.escape(label)}</span>')
        else:
            html_parts.append(f'<a href="{link}">{html.escape(label)}</a>')
    return '<div class="breadcrumb">' + " / ".join(html_parts) + "</div>"


# ── Pages ─────────────────────────────────────────────────────────────────────

ALLOWED_EXT = {".md", ".txt", ".yaml", ".yml", ".json", ".py", ".sh", ".log",
               ".csv", ".toml", ".cfg", ".ini", ".env", ".sql", ".html", ".css",
               ".js", ".ts", ".rs", ".go", ".java", ".c", ".h", ".cpp", ".rb",
               ".png", ".jpg", ".jpeg", ".svg", ".gif", ".ico",
               ".pdf", ".zip", ".tar.gz", ".tgz", ".gz", ".xz"}


def _page_send() -> str:
    allowed = ", ".join(sorted(ALLOWED_EXT))
    body = f"""<div class="upload-area" id="dropZone">
  <form id="uploadForm" enctype="multipart/form-data" method="post">
    <input type="file" name="file" id="fileInput" accept="{allowed}">
    <label for="fileInput" class="select-hint">📎 파일 선택</label>
    <p class="drop-hint">📂 여기에 놓으세요</p>
    <p id="fileName" class="uploaded-name"></p>
    <p class="name-error" id="nameError">⚠️ 허용되지 않는 파일 유형입니다</p>
    <button type="submit" class="btn" id="uploadBtn" disabled>업로드</button>
  </form>
  <div class="file-types"><b>허용:</b> {allowed}</div>
</div>
<script>
const dropZone = document.getElementById('dropZone');
const fileInput = document.getElementById('fileInput');
const fileName = document.getElementById('fileName');
const nameError = document.getElementById('nameError');
const uploadBtn = document.getElementById('uploadBtn');
const allowed = new Set({json.dumps(sorted(ALLOWED_EXT))});

function checkFile(f) {{
    const ext = '.' + f.name.split('.').slice(1).join('.');
    const ok = allowed.has(ext) || allowed.has(ext.toLowerCase());
    if (!ok) {{
        nameError.style.display = 'block';
        uploadBtn.disabled = true;
    }}
    return ok;
}}

dropZone.addEventListener('dragover', function(e) {{
    e.preventDefault();
    dropZone.classList.add('drag-over');
}});
dropZone.addEventListener('dragleave', function(e) {{
    e.preventDefault();
    dropZone.classList.remove('drag-over');
}});
dropZone.addEventListener('drop', function(e) {{
    e.preventDefault();
    dropZone.classList.remove('drag-over');
    nameError.style.display = 'none';
    if (e.dataTransfer.files.length > 0) {{
        const f = e.dataTransfer.files[0];
        if (checkFile(f)) {{
            fileInput.files = e.dataTransfer.files;
            fileName.textContent = f.name + ' (' + (f.size/1024).toFixed(1) + ' KB)';
            uploadBtn.disabled = false;
        }}
    }}
}});
fileInput.addEventListener('change', function() {{
    nameError.style.display = 'none';
    if (this.files.length > 0) {{
        const f = this.files[0];
        if (checkFile(f)) {{
            fileName.textContent = f.name + ' (' + (f.size/1024).toFixed(1) + ' KB)';
            uploadBtn.disabled = false;
        }}
    }}
}});
</script>"""
    return _page("보내기", body, "send", f"SAS links valid for {SAS_HOURS}h")


def _page_send_done(filename: str, blob_name: str, size: int) -> str:
    sas = _generate_sas(blob_name)
    body = f"""<div class="result">
  <p class="ok">✅ 업로드 완료</p>
  <p>파일: <b>{html.escape(filename)}</b> ({_size_fmt(size)})</p>
  <code>{html.escape(blob_name)}</code>
  <p style="margin-top:12px;"><a href="{sas}" style="color:#7ee787;">📥 다운로드 (SAS)</a></p>
  <p style="margin-top:12px;"><a href="/send" class="btn" style="text-decoration:none;display:inline-block;">추가 업로드</a></p>
</div>"""
    return _page("업로드 완료", body, "send", f"SAS valid for {SAS_HOURS}h")


def _page_receive(path: str) -> str:
    prefix = path.lstrip("/") + "/" if path else ""
    blobs = _list_blobs(prefix)
    tree = _virtual_tree(prefix, blobs)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    body = _breadcrumb(path)
    if not tree["dirs"] and not tree["files"]:
        body += '<div class="empty">📭 비어 있음</div>'
    else:
        body += "<table><tr><th>이름</th><th>크기</th></tr>"
        for d in tree["dirs"]:
            href = f"/receive/{prefix}{d['name']}"
            lbl = html.escape(d["name"]) + "/"
            cnt = f'<span class="count">({d["count"]} 항목, {_size_fmt(d["total_size"])})</span>'
            body += f'<tr class="dir"><td><a href="{href}">{lbl}</a> {cnt}</td><td class="size">—</td></tr>'
        for f in tree["files"]:
            sas_url = _generate_sas(prefix + f["name"])
            body += f'<tr class="file"><td><a href="{sas_url}">📄 {html.escape(f["name"])}</a></td><td class="size">{_size_fmt(f["size"])}</td></tr>'
        body += "</table>"
    return _page("받기", body, "receive", f"SAS links valid for {SAS_HOURS}h · {now}")


# ── HTTP Handler ──────────────────────────────────────────────────────────────
class BlobHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        if path == "/health":
            self._text(200, "OK")
            return

        # Redirect / to /receive
        if path == "/" or path == "":
            self._redirect("/receive")
            return

        # /send — upload form
        if path == "/send" or path == "/send/":
            self._html(_page_send())
            return

        # /receive — root listing
        if path == "/receive" or path == "/receive/":
            self._html(_page_receive(""))
            return

        # /receive/<path> — browse subdirectory
        if path.startswith("/receive/"):
            self._html(_page_receive(path[len("/receive/"):]))
            return

        self._text(404, "Not Found")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path

        # /send — handle file upload
        if path == "/send" or path == "/send/":
            ctype = self.headers.get("content-type", "")
            if "multipart/form-data" not in ctype:
                self._text(400, "multipart/form-data expected")
                return

            form = cgi.FieldStorage(fp=self.rfile, headers=self.headers,
                                     environ={"REQUEST_METHOD": "POST",
                                              "CONTENT_TYPE": ctype})
            item = form["file"] if "file" in form else None
            if item is None or not item.filename:
                self._html(_page("오류", '<div class="result"><p class="err">❌ 파일이 선택되지 않았습니다.</p><p><a href="/send">다시 시도</a></p></div>', "send"))
                return

            data = item.file.read()
            filename = Path(item.filename).name
            try:
                blob_name = _upload_blob(filename, data)
                self._html(_page_send_done(filename, blob_name, len(data)))
            except Exception as e:
                self._html(_page("오류", f'<div class="result"><p class="err">❌ 업로드 실패: {html.escape(str(e))}</p></div>', "send"))
            return

        self._text(404, "Not Found")

    def _redirect(self, location: str):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def _html(self, body: str):
        self._respond(200, body, "text/html; charset=utf-8")

    def _text(self, status: int, body: str):
        self._respond(status, body, "text/plain; charset=utf-8")

    def _respond(self, status: int, body: str, content_type: str):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()

    def log_message(self, fmt, *args):
        print(f"[blob] {self.address_string()} - {fmt % args}", file=sys.stderr, flush=True)


def main():
    host, port_text = LISTEN_ADDR.rsplit(":", 1)
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((host, int(port_text)), BlobHandler)
    print(f"[blob] listening on {host}:{port_text}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

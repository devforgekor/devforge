#!/usr/bin/env python3
# Status: production
"""HTTP handler + HTML UI for Blob Explorer."""

import cgi
import html
import json
import os
import sys
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from blob_explorer.blob import (
    _list_blobs, _virtual_tree, _generate_sas, _share_url, _upload_blob,
    presign_upload, SAS_HOURS,
)


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
<h1>\U0001f4e6 DevForge Blob <span>{subtitle}</span></h1>
<div class="nav"><a href="/send"{send_active}>\U0001f4e4 \ubcf4\ub0b4\uae30</a><a href="/receive"{recv_active}>\U0001f4e5 \ubc1b\uae30</a></div>
"""

PAGE_BOTTOM = '<div class="footer">{footer}</div></body></html>'

ALLOWED_EXT = {".md", ".txt", ".yaml", ".yml", ".json", ".py", ".sh", ".log",
               ".csv", ".toml", ".cfg", ".ini", ".env", ".sql", ".html", ".css",
               ".js", ".ts", ".rs", ".go", ".java", ".c", ".h", ".cpp", ".rb",
               ".png", ".jpg", ".jpeg", ".svg", ".gif", ".ico",
               ".pdf", ".zip", ".tar.gz", ".tgz", ".gz", ".xz"}


def _size_fmt(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    else:
        return f"{size / (1024 * 1024):.1f} MB"


def _page(subtitle: str, body: str, mode: str, footer: str = "") -> str:
    return PAGE_TOP.format(
        style=STYLE, subtitle=subtitle,
        send_active=' class="active"' if mode == "send" else "",
        recv_active=' class="active"' if mode == "receive" else "",
    ) + body + PAGE_BOTTOM.format(footer=footer)


def _breadcrumb(path: str) -> str:
    if not path:
        return '<div class="breadcrumb"><a href="/receive">\U0001f3e0 root</a></div>'
    parts = [p for p in path.split("/") if p]
    crumbs = [('\U0001f3e0 root', '/receive')]
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


def _page_send() -> str:
    allowed = ", ".join(sorted(ALLOWED_EXT))
    body = f"""<div class="upload-area" id="dropZone">
  <form id="uploadForm" enctype="multipart/form-data" method="post">
    <input type="file" name="file" id="fileInput" accept="{allowed}">
    <label for="fileInput" class="select-hint">\U0001f4ce \ud30c\uc77c \uc120\ud0dd</label>
    <p class="drop-hint">\U0001f4c2 \uc5ec\uae30\uc5d0 \ub193\uc73c\uc138\uc694</p>
    <p id="fileName" class="uploaded-name"></p>
    <p class="name-error" id="nameError">\u26a0\ufe0f \ud5c8\uc6a9\ub418\uc9c0 \uc54a\ub294 \ud30c\uc77c \uc720\ud615\uc785\ub2c8\ub2e4</p>
    <button type="submit" class="btn" id="uploadBtn" disabled>\uc5c5\ub85c\ub4dc</button>
  </form>
  <div class="file-types"><b>\ud5c8\uc6a9:</b> {allowed}</div>
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

const uploadForm = document.getElementById('uploadForm');
uploadForm.addEventListener('submit', async function(e) {{
    if (uploadForm.dataset.fallback === '1') return;
    e.preventDefault();
    const f = fileInput.files[0];
    if (!f) return;
    uploadBtn.disabled = true;
    try {{
        const pr = await fetch('/presign?name=' + encodeURIComponent(f.name)).then(function(r) {{ if (!r.ok) throw new Error('presign'); return r.json(); }});
        const put = await fetch(pr.upload_url, {{ method: 'PUT', body: f, headers: {{ 'Content-Type': f.type || 'application/octet-stream' }} }});
        if (!put.ok) throw new Error('put ' + put.status);
        document.body.innerHTML = '<div class="result"><p class="ok">\u2705 \uc5c5\ub85c\ub4dc \uc644\ub8cc (\uc9c1\uc811 \uc5c5\ub85c\ub4dc)</p><p>\ud30c\uc77c: <b>' + f.name + '</b> (' + (f.size/1024).toFixed(1) + ' KB)</p><code>' + pr.object_name + '</code><p style="margin-top:12px;"><a href="' + pr.download_url + '" style="color:#7ee787;">\U0001f4e5 \ub2e4\uc6b4\ub85c\ub4dc \ub9c1\ud06c</a></p><p style="margin-top:12px;"><a href="/send" class="btn" style="text-decoration:none;display:inline-block;">\ucd94\uac00 \uc5c5\ub85c\ub4dc</a></p></div>';
    }} catch (err) {{
        uploadForm.dataset.fallback = '1';
        uploadForm.submit();
    }}
}});
</script>"""
    return _page("\ubcf4\ub0b4\uae30", body, "send", f"SAS links valid for {SAS_HOURS}h")


def _page_send_done(filename: str, blob_name: str, size: int) -> str:
    sas = _share_url(blob_name)
    body = f"""<div class="result">
  <p class="ok">\u2705 \uc5c5\ub85c\ub4dc \uc644\ub8cc</p>
  <p>\ud30c\uc77c: <b>{html.escape(filename)}</b> ({_size_fmt(size)})</p>
  <code>{html.escape(blob_name)}</code>
  <p style="margin-top:12px;"><a href="{sas}" style="color:#7ee787;">\U0001f4e5 \ub2e4\uc6b4\ub85c\ub4dc \ub9c1\ud06c</a></p>
  <p style="margin-top:12px;"><a href="/send" class="btn" style="text-decoration:none;display:inline-block;">\ucd94\uac00 \uc5c5\ub85c\ub4dc</a></p>
</div>"""
    return _page("\uc5c5\ub85c\ub4dc \uc644\ub8cc", body, "send", f"SAS valid for {SAS_HOURS}h")


def _page_receive(path: str) -> str:
    prefix = path.lstrip("/") + "/" if path else ""
    blobs = _list_blobs(prefix)
    tree = _virtual_tree(prefix, blobs)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    body = _breadcrumb(path)
    if not tree["dirs"] and not tree["files"]:
        body += '<div class="empty">\U0001f4ed \ube48 \uacf3</div>'
    else:
        body += "<table><tr><th>\uc774\ub984</th><th>\ud06c\uae30</th></tr>"
        for d in tree["dirs"]:
            href = f"/receive/{prefix}{d['name']}"
            lbl = html.escape(d["name"]) + "/"
            cnt = f'<span class="count">({d["count"]} \ud56d\ubaa9, {_size_fmt(d["total_size"])})</span>'
            body += f'<tr class="dir"><td><a href="{href}">{lbl}</a> {cnt}</td><td class="size">\u2014</td></tr>'
        for f in tree["files"]:
            sas_url = _generate_sas(prefix + f["name"])
            body += f'<tr class="file"><td><a href="{sas_url}">\U0001f4c4 {html.escape(f["name"])}</a></td><td class="size">{_size_fmt(f["size"])}</td></tr>'
        body += "</table>"
    return _page("\ubc1b\uae30", body, "receive", f"SAS links valid for {SAS_HOURS}h \u00b7 {now}")


class BlobHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/health":
            self._text(200, "OK")
            return
        if path == "/" or path == "":
            self._redirect("/receive")
            return
        if path == "/send" or path == "/send/":
            self._html(_page_send())
            return
        if path == "/receive" or path == "/receive/":
            self._html(_page_receive(""))
            return
        if path.startswith("/receive/"):
            self._html(_page_receive(path[len("/receive/"):]))
            return
        if path == "/presign":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            name = (qs.get("name", [""])[0]).strip()
            if not name:
                self._json(400, {"error": "missing name"})
                return
            try:
                self._json(200, presign_upload(Path(name).name))
            except Exception as e:
                self._json(500, {"error": str(e)})
            return
        self._text(404, "Not Found")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
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
                self._html(_page("\uc624\ub958", '<div class="result"><p class="err">\u274c \ud30c\uc77c\uc774 \uc120\ud0dd\ub418\uc9c0 \uc54a\uc558\uc2b5\ub2c8\ub2e4.</p><p><a href="/send">\ub2e4\uc2dc \uc2dc\ub3c4</a></p></div>', "send"))
                return
            data = item.file.read()
            filename = Path(item.filename).name
            try:
                blob_name = _upload_blob(filename, data)
                self._html(_page_send_done(filename, blob_name, len(data)))
            except Exception as e:
                self._html(_page("\uc624\ub958", f'<div class="result"><p class="err">\u274c \uc5c5\ub85c\ub4dc \uc2e4\ud328: {html.escape(str(e))}</p></div>', "send"))
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

    def _json(self, status: int, obj: dict):
        self._respond(status, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

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

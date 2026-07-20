#!/usr/bin/env python3.11
# Status: experimental
"""HITL Review Dashboard — web UI for fact CONFIRM/REJECT.

Usage (standalone):
  python3 -m devforge_fastapi.review_dashboard [--port 9002]

Or mount as FastAPI router in app.py:
  from review_dashboard import router
  app.include_router(router, prefix="/review")
"""

import html
import json
import os
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.db import esc_sql, psql_json, psql_ok

STYLE = """<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, system-ui, sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; max-width: 1100px; margin: 0 auto; }
  h1 { color: #58a6ff; font-size: 1.3em; margin-bottom: 4px; }
  .subtitle { color: #8b949e; font-size: 0.85em; margin-bottom: 20px; }
  .stats { display: flex; gap: 12px; margin-bottom: 20px; flex-wrap: wrap; }
  .stat-card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 12px 20px; text-align: center; flex: 1; min-width: 100px; }
  .stat-card .num { font-size: 1.6em; font-weight: 700; }
  .stat-card .label { font-size: 0.78em; color: #8b949e; margin-top: 2px; }
  .stat-card.high .num { color: #7ee787; }
  .stat-card.medium .num { color: #d29922; }
  .stat-card.low .num { color: #f85149; }
  .stat-card.all .num { color: #58a6ff; }
  .stat-card.done .num { color: #7ee787; }
  .filters { display: flex; gap: 12px; margin-bottom: 16px; align-items: center; flex-wrap: wrap; }
  .filters label { color: #8b949e; font-size: 0.85em; }
  .filters select, .filters input { background: #0d1117; color: #c9d1d9; border: 1px solid #30363d; border-radius: 6px; padding: 6px 10px; font-size: 0.85em; }
  .filters .btn { background: #21262d; color: #c9d1d9; border: 1px solid #30363d; border-radius: 6px; padding: 6px 14px; cursor: pointer; font-size: 0.85em; }
  .filters .btn:hover { background: #30363d; }
  table { width: 100%; border-collapse: collapse; font-size: 0.88em; }
  th { text-align: left; padding: 10px 12px; border-bottom: 2px solid #30363d; color: #8b949e; font-weight: 600; font-size: 0.82em; text-transform: uppercase; letter-spacing: 0.04em; }
  td { padding: 10px 12px; border-bottom: 1px solid #21262d; vertical-align: top; }
  tr:hover { background: #161b22; }
  tr.done { opacity: 0.5; }
  .fact-triple { color: #f0f6fc; }
  .fact-triple .subj { color: #79c0ff; }
  .fact-triple .pred { color: #d2a8ff; }
  .fact-triple .obj { color: #7ee787; }
  .evidence { color: #8b949e; font-size: 0.85em; margin-top: 4px; max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .qc-list { font-size: 0.8em; margin-top: 4px; }
  .qc-list .pass { color: #7ee787; }
  .qc-list .fail { color: #f85149; }
  .qc-list .na { color: #484f58; }
  .score-badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-weight: 600; font-size: 0.85em; }
  .score-high { background: #1b3a1b; color: #7ee787; }
  .score-medium { background: #3a2f1b; color: #d29922; }
  .score-low { background: #3a1b1b; color: #f85149; }
  .btn-group { display: flex; gap: 6px; }
  .btn-confirm { background: #238636; color: #fff; border: none; padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 0.85em; }
  .btn-confirm:hover { background: #2ea043; }
  .btn-confirm:disabled { background: #1b3a1b; color: #484f58; cursor: not-allowed; }
  .btn-reject { background: #da3633; color: #fff; border: none; padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 0.85em; }
  .btn-reject:hover { background: #f85149; }
  .btn-reject:disabled { background: #3a1b1b; color: #484f58; cursor: not-allowed; }
  .verdict-badge { font-size: 0.8em; padding: 2px 8px; border-radius: 4px; }
  .verdict-CONFIRM { color: #7ee787; }
  .verdict-REJECT { color: #f85149; }
  .empty { text-align: center; color: #8b949e; padding: 40px; }
  .toast { position: fixed; bottom: 20px; right: 20px; padding: 12px 20px; border-radius: 8px; font-size: 0.9em; z-index: 100; transition: opacity .3s; }
  .toast.ok { background: #1b3a1b; color: #7ee787; border: 1px solid #238636; }
  .toast.err { background: #3a1b1b; color: #f85149; border: 1px solid #da3633; }
  .footer { margin-top: 24px; font-size: 0.8em; color: #484f58; text-align: center; }
</style>"""

HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fact Review Dashboard</title>
{style}
</head>
<body>
<h1>Fact Review Dashboard</h1>
<p class="subtitle">Human-in-the-loop review for extract_pipeline facts</p>

<div class="stats" id="stats"></div>

<div class="filters">
  <label>Score:</label>
  <select id="filterScore">
    <option value="all">All</option>
    <option value="medium" selected>Medium (50-89)</option>
    <option value="high">High (90-100)</option>
    <option value="low">Low (0-49)</option>
  </select>
  <label>Verdict:</label>
  <select id="filterVerdict">
    <option value="all">All</option>
    <option value="pending" selected>Pending</option>
    <option value="confirmed">Confirmed</option>
    <option value="rejected">Rejected</option>
  </select>
  <button class="btn" onclick="loadFacts()">Refresh</button>
  <span id="countInfo" style="color:#8b949e;font-size:0.85em;margin-left:8px;"></span>
</div>

<table>
<thead><tr>
  <th style="width:35%">Fact</th>
  <th style="width:8%">Score</th>
  <th style="width:12%">QC</th>
  <th style="width:8%">Verdict</th>
  <th style="width:10%">Model</th>
  <th style="width:12%">Action</th>
</tr></thead>
<tbody id="factsBody"></tbody>
</table>
<div id="emptyMessage" class="empty" style="display:none;">No facts match.</div>
<div id="toast" class="toast" style="opacity:0;"></div>
<div class="footer">DevForge HITL Review</div>

<script>
let currentFacts = [];

function showToast(msg, type) {{
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast ' + type;
  t.style.opacity = '1';
  setTimeout(() => {{ t.style.opacity = '0'; }}, 3000);
}}

async function loadFacts() {{
  const params = new URLSearchParams({{
    score: document.getElementById('filterScore').value,
    verdict: document.getElementById('filterVerdict').value
  }});
  try {{
    const resp = await fetch('/api/facts?' + params);
    const data = await resp.json();
    currentFacts = data.facts || [];
    renderFacts(currentFacts);
    renderStats(data.stats);
    document.getElementById('countInfo').textContent = currentFacts.length + ' facts';
  }} catch(e) {{
    showToast('Load failed: ' + e.message, 'err');
  }}
}}

function renderStats(stats) {{
  const el = document.getElementById('stats');
  if (!stats) {{ el.innerHTML = ''; return; }}
  el.innerHTML = `
    <div class="stat-card all"><div class="num">${{stats.all || 0}}</div><div class="label">Total</div></div>
    <div class="stat-card high"><div class="num">${{stats.high || 0}}</div><div class="label">High (>=90)</div></div>
    <div class="stat-card medium"><div class="num">${{stats.medium || 0}}</div><div class="label">Medium (50-89)</div></div>
    <div class="stat-card low"><div class="num">${{stats.low || 0}}</div><div class="label">Low (<50)</div></div>
    <div class="stat-card done"><div class="num">${{stats.reviewed || 0}}</div><div class="label">Reviewed</div></div>`;
}}

function renderFacts(facts) {{
  const tbody = document.getElementById('factsBody');
  const empty = document.getElementById('emptyMessage');
  if (!facts.length) {{ tbody.innerHTML = ''; empty.style.display = 'block'; return; }}
  empty.style.display = 'none';
  tbody.innerHTML = facts.map(f => renderRow(f)).join('');
}}

function renderRow(f) {{
  const score = f.quality_score;
  const sc = score >= 90 ? 'score-high' : score >= 50 ? 'score-medium' : 'score-low';
  const qcHtml = renderQC(f.quality_checks);
  const done = f.user_verdict !== null;
  const vHtml = done
    ? '<span class="verdict-badge verdict-' + f.user_verdict + '">' + f.user_verdict + '</span>'
    : '<span style="color:#484f58;">\u2014</span>';
  const ev = (f.evidence || '').slice(0, 140);
  const ts = f.created_at ? new Date(f.created_at).toLocaleDateString() : '';
  const btnHtml = done ? '' :
    '<div class="btn-group">' +
    '<button class="btn-confirm" onclick="act(\'' + f.id + '\',\'confirm\')">Confirm</button>' +
    '<button class="btn-reject" onclick="act(\'' + f.id + '\',\'reject\')">Reject</button></div>';
  return '<tr class="' + (done ? 'done' : '') + '">' +
    '<td><div class="fact-triple"><span class="subj">' + esc(f.subject) + '</span>' +
    ' <span style="color:#484f58;">--</span> <span class="pred">' + esc(f.predicate) + '</span>' +
    ' <span style="color:#484f58;">-></span> <span class="obj">' + esc(f.object) + '</span></div>' +
    '<div class="evidence" title="' + esc(ev) + '">' + esc(ev) + '</div></td>' +
    '<td><span class="score-badge ' + sc + '">' + score + '</span></td>' +
    '<td><div class="qc-list">' + qcHtml + '</div></td>' +
    '<td>' + vHtml + '</td>' +
    '<td style="font-size:0.8em;color:#8b949e;">' + esc(f.extract_model || '') + '<br>' + ts + '</td>' +
    '<td>' + btnHtml + '</td></tr>';
}}

function renderQC(qc) {{
  if (!qc) return '<span class="na">\u2014</span>';
  const checks = qc._qc_checks || qc;
  const items = [];
  for (const [k, v] of Object.entries(checks)) {{
    if (k === 'quality_score') continue;
    const vs = String(v).toLowerCase();
    const cls = (vs === 'passed' || vs === 'n/a' || vs === 'true') ? 'pass'
      : (vs === 'low_confidence' || vs === 'failed' || vs === 'reversal') ? 'fail' : 'na';
    items.push('<span class="' + cls + '">' + k.replace(/_/g, ' ') + ': ' + v + '</span>');
  }}
  return items.join('<br>') || '<span class="na">\u2014</span>';
}}

async function act(id, action) {{
  try {{
    const resp = await fetch('/api/' + id + '/' + action, {{ method: 'POST' }});
    const data = await resp.json();
    if (data.ok) {{ showToast(action === 'confirm' ? 'Confirmed' : 'Rejected', 'ok'); loadFacts(); }}
    else {{ showToast('Error: ' + (data.error || 'unknown'), 'err'); }}
  }} catch(e) {{ showToast('Request failed: ' + e.message, 'err'); }}
}}

function esc(s) {{
  if (!s) return '';
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}}

loadFacts();
</script>
</body>
</html>"""
HTML = HTML.format(style=STYLE)


def _parse_score(qc) -> float:
    if not qc:
        return 0.0
    if isinstance(qc, dict):
        s = qc.get("quality_score")
        if s is not None:
            return float(s)
        inner = qc.get("_qc_checks", {})
        s = inner.get("quality_score")
        if s is not None:
            return float(s)
    return 0.0


def _parse_qc(qc) -> dict:
    if not qc:
        return {}
    if isinstance(qc, dict):
        inner = qc.get("_qc_checks")
        return inner if isinstance(inner, dict) else qc
    return {}


def _load_facts(score_filter="medium", verdict_filter="pending", limit=100):
    clauses = ["rf.source = 'extract_pipeline'", "rf.quality_checks IS NOT NULL"]
    if verdict_filter == "pending":
        clauses.append("rf.user_verdict IS NULL")
    elif verdict_filter == "confirmed":
        clauses.append("rf.user_verdict = 'CONFIRM'")
    elif verdict_filter == "rejected":
        clauses.append("rf.user_verdict = 'REJECT'")

    rows = psql_json(f"""
        SELECT rf.id::text, rf.fact_index, rf.subject, rf.predicate, rf.object,
               rf.quality_checks, rf.user_verdict, rf.user_verdict_at,
               rf.extract_model, rf.created_at, rf.verdict,
               LEFT(rf.evidence, 200) AS evidence
        FROM review_facts rf
        WHERE {' AND '.join(clauses)}
        ORDER BY rf.created_at DESC
        LIMIT {limit}
    """)

    facts = []
    for r in rows:
        qc = r.get("quality_checks", {})
        score = _parse_score(qc)
        facts.append({
            "id": r["id"],
            "fact_index": r["fact_index"],
            "subject": r.get("subject", "") or "",
            "predicate": r.get("predicate", "") or "",
            "object": r.get("object", "") or "",
            "quality_score": score,
            "quality_checks": _parse_qc(qc),
            "user_verdict": r.get("user_verdict"),
            "user_verdict_at": str(r.get("user_verdict_at") or ""),
            "extract_model": r.get("extract_model", "") or "",
            "created_at": str(r.get("created_at") or ""),
            "evidence": r.get("evidence", "") or "",
            "verdict": r.get("verdict", "") or "",
        })

    if score_filter == "high":
        facts = [f for f in facts if f["quality_score"] >= 90]
    elif score_filter == "medium":
        facts = [f for f in facts if 50 <= f["quality_score"] < 90]
    elif score_filter == "low":
        facts = [f for f in facts if f["quality_score"] < 50]

    return facts


def _load_stats():
    rows = psql_json("""
        SELECT
            COUNT(*) AS all_count,
            COUNT(*) FILTER (WHERE (quality_checks->>'quality_score')::numeric >= 90) AS high_count,
            COUNT(*) FILTER (WHERE (quality_checks->>'quality_score')::numeric >= 50 AND (quality_checks->>'quality_score')::numeric < 90) AS medium_count,
            COUNT(*) FILTER (WHERE (quality_checks->>'quality_score')::numeric < 50) AS low_count,
            COUNT(*) FILTER (WHERE user_verdict IS NOT NULL) AS reviewed_count
        FROM review_facts
        WHERE source = 'extract_pipeline' AND quality_checks IS NOT NULL
    """)
    r = rows[0] if rows else {}
    return {
        "all": r.get("all_count", 0),
        "high": r.get("high_count", 0),
        "medium": r.get("medium_count", 0),
        "low": r.get("low_count", 0),
        "reviewed": r.get("reviewed_count", 0),
    }


def _set_verdict(fact_id: str, verdict: str) -> bool:
    fact = psql_json(f"""
        SELECT rf.id, rf.evidence,
           CASE rf.fact_type
             WHEN 'user' THEN t.user_turn
             WHEN 'thinking' THEN t.thinking
             WHEN 'text' THEN t.text
           END AS source_text,
           rf.fact_type
        FROM review_facts rf
        JOIN turns t ON t.id = rf.turn_id
        WHERE rf.id = '{esc_sql(fact_id)}'
    """)
    if not fact:
        return False
    if not psql_ok(
        f"UPDATE review_facts SET user_verdict='{verdict}', user_verdict_at=NOW() WHERE id='{esc_sql(fact_id)}'"
    ):
        return False
    r = fact[0]
    ev = esc_sql(r.get("evidence", ""))
    src = esc_sql(r.get("source_text", ""))
    ft = esc_sql(r.get("fact_type", ""))
    psql_ok(f"""INSERT INTO feedback_examples (evidence_text, source_text, fact_type, verdict)
       VALUES ('{ev}', '{src}', '{ft}', '{verdict}')
       ON CONFLICT DO NOTHING""")
    return True


class ReviewHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = parse_qs(parsed.query)

        if path == "/" or path == "/review":
            self._html(HTML)
        elif path == "/api/facts":
            score = params.get("score", ["medium"])[0]
            verdict = params.get("verdict", ["pending"])[0]
            facts = _load_facts(score, verdict)
            stats = _load_stats()
            self._json(200, {"facts": facts, "stats": stats})
        elif path.startswith("/api/") and path.endswith("/confirm"):
            fid = path.split("/")[2]
            ok = _set_verdict(fid, "CONFIRM")
            self._json(200, {"ok": ok, "verdict": "CONFIRM"} if ok else {"ok": False, "error": "not found"})
        elif path.startswith("/api/") and path.endswith("/reject"):
            fid = path.split("/")[2]
            ok = _set_verdict(fid, "REJECT")
            self._json(200, {"ok": ok, "verdict": "REJECT"} if ok else {"ok": False, "error": "not found"})
        elif path == "/health":
            self._text(200, "OK")
        else:
            self._text(404, "Not Found")

    def do_POST(self):
        self.do_GET()

    def _html(self, body: str):
        self._respond(200, body, "text/html; charset=utf-8")

    def _json(self, status: int, data: dict):
        self._respond(status, json.dumps(data, ensure_ascii=False), "application/json; charset=utf-8")

    def _text(self, status: int, body: str):
        self._respond(status, body, "text/plain; charset=utf-8")

    def _respond(self, status: int, body: str, ctype: str):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()

    def log_message(self, fmt, *args):
        print(f"[review] {fmt % args}", file=sys.stderr, flush=True)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Review Dashboard Server")
    parser.add_argument("--port", "-p", type=int, default=9002)
    parser.add_argument("--host", type=str, default="127.0.0.1")
    args = parser.parse_args()
    server = HTTPServer((args.host, args.port), ReviewHandler)
    print(f"[review] Dashboard: http://{args.host}:{args.port}/review")
    print(f"[review] API:      http://{args.host}:{args.port}/api/facts")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[review] Shutting down")
        server.server_close()


if __name__ == "__main__":
    main()

# ── FastAPI router (for production — imported by app.py) ──────────

try:
    from fastapi import APIRouter, Query
    from fastapi.responses import HTMLResponse, JSONResponse

    router = APIRouter(prefix="/review")

    @router.get("")
    async def review_page():
        return HTMLResponse(HTML)

    @router.get("/api/facts")
    async def get_facts(score: str = Query("medium"), verdict: str = Query("pending"), limit: int = Query(100)):
        facts = _load_facts(score, verdict, limit)
        stats = _load_stats()
        return {"facts": facts, "stats": stats}

    @router.post("/api/{fact_id}/confirm")
    async def confirm_fact(fact_id: str):
        ok = _set_verdict(fact_id, "CONFIRM")
        if not ok:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        return {"ok": True, "verdict": "CONFIRM"}

    @router.post("/api/{fact_id}/reject")
    async def reject_fact(fact_id: str):
        ok = _set_verdict(fact_id, "REJECT")
        if not ok:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        return {"ok": True, "verdict": "REJECT"}

except ImportError:
    router = None

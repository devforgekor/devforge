#!/usr/bin/env python3
# Status: production
# Path: systemd:activity-summarizer-safety.timer → activity-summarizer.service
"""activity_summarizer.py — Daily LLM summarization of activity_log events.

Runs via systemd timer at KST 06:00 (21:00 UTC).
Reads all raw events (summary_status='raw'), groups by run_id, sends to
Pod B (MODEL_REGISTRY proposer) for summarization, inserts 'summary' rows,
marks source rows as 'summarized'.

Single file, no new dependencies. ~250 lines.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from lib.db import psql_json, psql, psql_ok, esc_sql
from lib.llm_client import MODEL_REGISTRY

LLAMA_HOST = "127.0.0.1"
LLAMA_PORT = MODEL_REGISTRY['proposer']['port']
FIVE_MIN = "INTERVAL '5 minutes'"
SUMMARY_TEMP = 0.0
SUMMARY_MAX_TOKENS = 1024
HTTP_TIMEOUT = 600
BODY_MAX_CHARS = 500

SENTINEL_DIR = "/var/tmp"


def fetch_raw_events() -> list:
    """Query raw events. Body included — critical for meaningful summarization."""
    sql = f"""SELECT id, type, source, title, summary, body,
                     agent, model, created_at, run_id, tags
              FROM activity_log
              WHERE summary_status = 'raw'
                AND created_at < NOW() - {FIVE_MIN}
              ORDER BY created_at ASC"""
    rows = psql_json(sql, timeout=30)
    if not rows:
        return []

    events = []
    for row in rows:
        body = row.get("body")
        if not isinstance(body, dict):
            body = {"raw": str(body)[:500]} if body else {}
        events.append({
            "id": row["id"], "type": row["type"], "source": row["source"],
            "title": row["title"], "summary": row["summary"], "body": body,
            "agent": row["agent"], "model": row["model"], "created_at": row["created_at"],
            "run_id": row["run_id"], "tags": row.get("tags", ""),
        })
    return events


def build_prompt(events: list) -> str:
    """Build LLM prompt from raw events. Truncate body if too long."""
    lines = []
    for e in events:
        body_str = json.dumps(e["body"], ensure_ascii=False)
        if len(body_str) > BODY_MAX_CHARS:
            body_str = body_str[:BODY_MAX_CHARS] + "..."
        lines.append(
            f"[{e['id']}] type={e['type']} source={e['source']} "
            f"agent={e.get('agent','-')} model={e.get('model','-')} "
            f"run_id={e.get('run_id','-')} "
            f"title: {e['title']}\n"
            f"summary: {e['summary']}\n"
            f"body: {body_str}"
        )
    return "\n---\n".join(lines)


SYSTEM_PROMPT = """You are a work log summarizer. Output ONLY valid JSON array.
Group events by run_id first. Within each day, group related events logically.
Each entry: {"date": "YYYY-MM-DD", "title": "...", "summary": "...", "tags": ["..."]}.
Ensure every day with events has at least one summary entry.
No markdown, no explanation — JSON array only."""


def call_llm(prompt: str) -> list:
    """Call llama.cpp (MODEL_REGISTRY proposer). Returns parsed JSON list on success, None on failure."""
    import http.client

    body = json.dumps({
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": SUMMARY_TEMP,
        "max_tokens": SUMMARY_MAX_TOKENS,
        "stream": False,
    })
    try:
        conn = http.client.HTTPConnection(LLAMA_HOST, LLAMA_PORT, timeout=HTTP_TIMEOUT)
        conn.request("POST", "/v1/chat/completions", body,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        conn.close()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1]
            if content.endswith("```"):
                content = content[:-3]
            content = content.strip()
        return json.loads(content)
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"  JSON parse error for LLM response: {e}")
        return None
    except Exception as e:
        print(f"  LLM call failed: {e}")
        return None



def mark_parse_failed(event_ids: list):
    """Mark rows as parse_failed. Next run retries with shorter prompt."""
    if not event_ids:
        return
    ids = ",".join(str(int(i)) for i in event_ids if i.isdigit() or (i.startswith("-") and i[1:].isdigit()))
    if not ids:
        return
    psql_ok(f"UPDATE activity_log SET summary_status='parse_failed' WHERE id IN ({ids})")


def insert_summaries(summaries: list):
    """Insert summary entries into activity_log."""
    count = 0
    for s in summaries:
        title = esc_sql(s.get("title", "")[:200])
        summary = esc_sql(s.get("summary", "")[:1000])
        date = esc_sql(s.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d")))
        tags_str = ",".join(f"'{esc_sql(t)}'" for t in s.get("tags", []))
        tags_sql = f"ARRAY[{tags_str}]" if tags_str else "'{}'"
        ok = psql_ok(f"""INSERT INTO activity_log (type, source, title, summary, tags, summary_status)
            VALUES ('summary', 'cli', '{title}', '{summary}', {tags_sql}, 'done')""")
        if ok:
            count += 1
    if count:
        print(f"  Inserted {count} summary rows")
    return count


def mark_summarized(event_ids: list):
    """Mark source rows as summarized."""
    if not event_ids:
        return
    ids = ",".join(str(int(i)) for i in event_ids if i.isdigit() or (i.startswith("-") and i[1:].isdigit()))
    if not ids:
        return
    ok = psql_ok(f"""UPDATE activity_log SET summary_status='summarized'
        WHERE id IN ({ids}) AND summary_status='raw'""")
    if ok:
        print(f"  Marked {len(event_ids)} rows as summarized")


def retry_parse_failed():
    """Retry previously failed rows with shorter (title-only) prompt."""
    sql = f"""SELECT id, type, source, title, agent, created_at
              FROM activity_log
              WHERE summary_status = 'parse_failed'
                AND created_at < NOW() - {FIVE_MIN}
              ORDER BY created_at ASC
              LIMIT 50"""
    rows = psql_json(sql, timeout=30)
    if not rows:
        return

    events = [{
        "id": row["id"], "type": row["type"], "source": row["source"],
        "title": row["title"], "agent": row["agent"], "created_at": row["created_at"],
    } for row in rows]

    if not events:
        return

    prompt = "Events (title only, no body):\n" + "\n".join(
        f"- [{e['id']}] {e['type']}: {e['title']}" for e in events
    )
    summaries = call_llm(prompt)
    if summaries is None:
        return  # Retry next cycle

    insert_summaries(summaries)
    mark_summarized([e["id"] for e in events])
    print(f"  Retried {len(events)} parse_failed rows")


def main():
    print(f"[{datetime.now(timezone.utc).isoformat()}] activity_summarizer starting...")

    # ── Guard: skip if already completed today ──────────────────
    sentinel = os.path.join(
        SENTINEL_DIR,
        f"devforge-summarizer-{datetime.now(timezone.utc).strftime('%Y%m%d')}.done",
    )
    if os.path.exists(sentinel):
        print(f"  Summarizer already done today. Sentinel exists. Skipping.")
        return 0

    # Cleanup old sentinels (>3 days)
    import glob
    for f in glob.glob(os.path.join(SENTINEL_DIR, "devforge-summarizer-*.done")):
        try:
            if os.path.getmtime(f) < datetime.now().timestamp() - 3 * 86400:
                os.remove(f)
        except OSError:
            pass

    success = False  # only set sentinel on success

    # 1. Fetch raw events
    events = fetch_raw_events()
    if not events:
        print("  No raw events. Nothing to summarize.")
        # Still retry previously failed rows
        retry_parse_failed()
        success = True
        Path(sentinel).touch()
        return 0

    print(f"  Fetched {len(events)} raw events")

    # 2. Build prompt
    prompt = build_prompt(events)
    print(f"  Prompt: {len(prompt)} chars")

    # 3. Call LLM
    summaries = call_llm(prompt)
    if summaries is None:
        print("  LLM call failed — retry next cycle")
        mark_parse_failed([e["id"] for e in events])
        retry_parse_failed()
        return 0

    # 4. Insert summaries
    if not insert_summaries(summaries):
        print("  No summaries inserted — marking parse_failed")
        mark_parse_failed([e["id"] for e in events])
        retry_parse_failed()
        return 0

    # 5. Mark source rows as summarized
    mark_summarized([e["id"] for e in events])

    # 6. Retry previously failed rows
    retry_parse_failed()

    success = True
    Path(sentinel).touch()
    print(f"[{datetime.now(timezone.utc).isoformat()}] Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())


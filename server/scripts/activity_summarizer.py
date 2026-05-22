"""activity_summarizer.py — Daily LLM summarization of activity_log events.

Runs via systemd timer at KST 07:30 (22:30 UTC).
Reads all raw events (summary_status='raw'), groups by run_id, sends to
phi-4-mini (Podman B :8081) for summarization, inserts 'summary' rows,
marks source rows as 'summarized'.

Single file, no new dependencies. ~250 lines.
"""

import json
import os
import sys
import http.client
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.db import psql, psql_ok, esc_sql

LLAMA_HOST = "127.0.0.1"
LLAMA_PORT = 8081
FIVE_MIN = "INTERVAL '5 minutes'"
SUMMARY_TEMP = 0.0
SUMMARY_MAX_TOKENS = 1024
HTTP_TIMEOUT = 600
BODY_MAX_CHARS = 500


def fetch_raw_events() -> list:
    """Query raw events. Body included — critical for meaningful summarization."""
    sql = f"""SELECT id, type, source, title, summary, body,
                     agent, model, created_at, run_id, tags
              FROM activity_log
              WHERE summary_status = 'raw'
                AND created_at < NOW() - {FIVE_MIN}
              ORDER BY created_at ASC"""
    raw = psql(sql, timeout=30)
    if not raw:
        return []

    events = []
    for line in raw.split("\n"):
        if not line.strip():
            continue
        parts = line.split("|", 11)
        if len(parts) < 10:
            continue
        try:
            body = json.loads(parts[5]) if parts[5] else {}
        except json.JSONDecodeError:
            body = {"raw": parts[5][:500]}
        events.append({
            "id": parts[0], "type": parts[1], "source": parts[2],
            "title": parts[3], "summary": parts[4], "body": body,
            "agent": parts[6], "model": parts[7], "created_at": parts[8],
            "run_id": parts[9], "tags": parts[10] if len(parts) > 10 else "",
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
    """Call llama.cpp at 127.0.0.1:8081. Returns parsed JSON list on success, [] on failure."""
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
        # Strip markdown fences if present
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1]
            if content.endswith("```"):
                content = content[:-3]
            content = content.strip()
        return json.loads(content)
    except json.JSONDecodeError as e:
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
    raw = psql(sql, timeout=30)
    if not raw:
        return

    events = []
    for line in raw.split("\n"):
        parts = line.split("|")
        if len(parts) >= 6:
            events.append({
                "id": parts[0], "type": parts[1], "source": parts[2],
                "title": parts[3], "agent": parts[4], "created_at": parts[5],
            })

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

    # 1. Fetch raw events
    events = fetch_raw_events()
    if not events:
        print("  No raw events. Nothing to summarize.")
        # Still retry previously failed rows
        retry_parse_failed()
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
        # Also retry old parse_failed rows while we're here
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

    print(f"[{datetime.now(timezone.utc).isoformat()}] Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

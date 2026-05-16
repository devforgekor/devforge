"""parser_claude.py — extract turns from Claude Code session JSONL."""
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ACTIVE_THRESHOLD_S = 30


def parse(path: Path) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str], bool]:
    """Parse a Claude Code session JSONL.
    Returns (turns, model, is_active).
    """
    mtime = path.stat().st_mtime
    is_active = (time.time() - mtime) < ACTIVE_THRESHOLD_S

    events = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            events.append(ev)

    turns = []
    current_user = None
    model = None

    for ev in events:
        if ev.get("isSidechain"):
            continue
        t = ev.get("type")
        if t == "user":
            content = ev.get("message", {}).get("content", "")
            if isinstance(content, list):
                continue
            current_user = content
        elif t == "assistant":
            msg = ev.get("message", {})
            model = msg.get("model", model)
            assistant_uuid = ev.get("uuid")
            content_blocks = msg.get("content", [])
            if not isinstance(content_blocks, list):
                content_blocks = []

            reasoning_parts = []
            answer_parts = []
            for block in content_blocks:
                if block.get("type") == "thinking":
                    reasoning_parts.append(block.get("thinking", ""))
                elif block.get("type") == "text":
                    answer_parts.append(block.get("text", ""))

            reasoning = "\n".join(reasoning_parts) or None
            answer = "\n".join(answer_parts) or ""

            if current_user is not None:
                turns.append({
                    "user_query": current_user,
                    "reasoning": reasoning,
                    "assistant_answer": answer,
                    "source_message_id": assistant_uuid,
                    "created_at": ev.get("timestamp"),
                })
                current_user = None

    # Active session: only drop last turn if assistant was writing (not user typing)
    if is_active and turns and current_user is None:
        turns.pop()

    return (turns if turns else None), model, is_active

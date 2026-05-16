"""parser_qwen.py — extract turns from Qwen Code CLI session JSONL."""
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ACTIVE_THRESHOLD_S = 30


def parse(path: Path) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str], bool]:
    """Parse a Qwen Code CLI chat JSONL.

    Format:
      Events with type=user, type=assistant, type=system.
      Content in message.parts[].text.
      Assistant has model field and role="model".

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
            if ev.get("type") == "system":
                continue
            events.append(ev)

    if len(events) < 2:
        return None, None, is_active

    turns = []
    current_user = None
    model = None

    for ev in events:
        t = ev.get("type")

        if t == "user":
            parts = ev.get("message", {}).get("parts", [])
            texts = [p.get("text", "") for p in parts if isinstance(p, dict)]
            current_user = "\n".join(texts)

        elif t == "assistant":
            model = ev.get("model", model)
            parts = ev.get("message", {}).get("parts", [])
            answer = "\n".join(
                p.get("text", "") for p in parts if isinstance(p, dict)
            )

            if current_user is not None and answer:
                turns.append({
                    "user_query": current_user,
                    "assistant_answer": answer,
                    "source_message_id": ev.get("uuid"),
                    "created_at": ev.get("timestamp"),
                })
                current_user = None

    # Active session: drop last turn if assistant just finished
    if is_active and turns and current_user is None:
        turns.pop()

    return (turns if turns else None), model, is_active

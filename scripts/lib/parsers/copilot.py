#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""parser_copilot.py — extract turns from Copilot events.jsonl."""
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ACTIVE_THRESHOLD_S = 30


def parse(path: Path) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str], bool]:
    """Parse a Copilot events.jsonl.
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
    current_interaction_id = None
    current_reasoning: List[str] = []
    current_answer: List[str] = []
    in_turn = False
    model = None

    for ev in events:
        t = ev.get("type")
        data = ev.get("data", {})

        if t == "user.message":
            if current_user is not None and (current_answer or current_reasoning):
                turns.append({
                    "user_turn": current_user,
                    "thinking": "\n".join(current_reasoning) or None,
                    "text": "\n".join(current_answer) or "",
                    "source_message_id": current_interaction_id,
                    "created_at": ev.get("timestamp"),
                })
            current_user = data.get("content", "")
            current_interaction_id = data.get("interactionId")
            current_reasoning = []
            current_answer = []
            in_turn = False

        elif t == "assistant.turn_start":
            in_turn = True
            current_reasoning = []
            current_answer = []

        elif t == "assistant.message":
            model = data.get("model", model)
            if data.get("reasoningText"):
                current_reasoning.append(data["reasoningText"])
            content = data.get("content", "")
            if content:
                current_answer.append(content)

        elif t == "assistant.turn_end":
            if current_user is not None and in_turn:
                turns.append({
                    "user_turn": current_user,
                    "thinking": "\n".join(current_reasoning) or None,
                    "text": "\n".join(current_answer) or "",
                    "source_message_id": current_interaction_id,
                    "created_at": ev.get("timestamp"),
                })
                current_user = None
            in_turn = False
            current_reasoning = []
            current_answer = []

    if current_user is not None and (current_answer or current_reasoning):
        turns.append({
            "user_turn": current_user,
            "thinking": "\n".join(current_reasoning) or None,
            "text": "\n".join(current_answer) or "",
            "source_message_id": current_interaction_id,
            "created_at": events[-1].get("timestamp") if events else None,
        })

    # Active session: only drop last turn if assistant was writing, not user typing
    if is_active and turns and current_user is None:
        turns.pop()

    return (turns if turns else None), model, is_active


#!/usr/bin/env python3
# Status: experimental
# Path: none — 1회성 Slack embed progress report (transferred from pipelines/)
"""Slack embed progress reporter — send Qwen 8B embedding status to Slack every 30min."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
from lib.db import psql_json
from lib.pipeline_common import slack_send

# Get progress
total = psql_json("SELECT COUNT(*) as cnt FROM turns") or [{"cnt": 0}]
done = psql_json("SELECT COUNT(*) as cnt FROM turns WHERE embedding IS NOT NULL") or [{"cnt": 0}]
remaining = psql_json("SELECT COUNT(*) as cnt FROM turns WHERE embedding IS NULL") or [{"cnt": 0}]

t = total[0]["cnt"]
d = done[0]["cnt"]
r = remaining[0]["cnt"]
pct = round(d / t * 100, 1) if t > 0 else 0

# Bar
bar_len = 16
filled = int(bar_len * d / t) if t > 0 else 0
bar = "█" * filled + "░" * (bar_len - filled)

msg = f"*Embed Progress* — Qwen3-Embedding-8B\n{bar} {pct}%  ({d}/{t} 완료, {r} 남음)"

if pct >= 100:
    msg += "\n✅ Embedding *complete* — day-cycle 타이머 활성화 준비 완료"

slack_send(msg)

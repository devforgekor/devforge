# Status: experimental
# Path: lib/watchdog/messenger.py
"""Watchdog Messenger — 정보 중개 시스템.

- 에러 메시지, 피드백, 사용자 전달 메시지 수집/배달.
- 파일 기반 큐 (/opt/ai_data/watchdog/messages.jsonl).
"""

import json
import os
import time
from datetime import datetime, timezone

MESSAGE_PATH = "/opt/ai_data/watchdog/messages.jsonl"
MAX_MESSAGES = 1000

def log_message(source: str, target: str, type: str, content: str, detail: str = ""):
    """메시지 기록 (Source: LLM/Pipeline/User, Target: Operator/User/Watchman)"""
    os.makedirs(os.path.dirname(MESSAGE_PATH), exist_ok=True)
    
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "target": target,
        "type": type,
        "content": content,
        "detail": detail,
        "delivered": False
    }
    
    with open(MESSAGE_PATH, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

def get_undelivered(target: str = None) -> list:
    """미전달 메시지 조회 및 마킹"""
    if not os.path.exists(MESSAGE_PATH):
        return []
        
    messages = []
    undelivered_indices = []
    
    with open(MESSAGE_PATH, "r") as f:
        lines = f.readlines()
        
    updated_lines = []
    for line in lines:
        try:
            msg = json.loads(line)
            if not msg.get("delivered") and (target is None or msg.get("target") == target):
                messages.append(msg)
                msg["delivered"] = True
                msg["delivered_ts"] = datetime.now(timezone.utc).isoformat()
            updated_lines.append(json.dumps(msg, ensure_ascii=False) + "\n")
        except:
            continue
            
    # 원자적 업데이트가 아니지만 파일이 작으므로 일단 단순 덮어쓰기
    with open(MESSAGE_PATH, "w") as f:
        f.writelines(updated_lines[-MAX_MESSAGES:])
        
    return messages

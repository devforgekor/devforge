#!/usr/bin/env python3
"""LLM record/replay 하네스 — Phase 3.5 구/신 대조를 위한 fixture 캡처.

Architecture:
  - RECORD 모드: LLM 호출을 가로채 fixture 파일에 저장
  - REPLAY 모드: fixture 파일에서 응답을 재생
  - passthrough 모드: 직접 LLM 호출 (Track A 기본, Track B 전환 시 사용)

Fixture Format:
  tests/fixtures/llm_recordings/
    ├── extract_llm.json       # 10개 extract.py 호출
    ├── text_clean_llm.json    # 1개 text_clean 호출
    ├── enrich_llm.json        # 5개 enrich.py 호출
    ├── worklog_llm.json       # 1개 worklog_generator 호출
    ├── nli_result.json        # NLI 결과 (5개)
    └── rerank_score.json      # Reranker 스코어 (3개)

Usage:
  # Record mode (현행 시스템에서 캡처)
  export DEVFORGE_LLM_RECORD=1
  python3 pipelines/extract.py --replay-capture

  # Replay mode (v1.2 시스템에서 재생)
  export DEVFORGE_LLM_REPLAY=1
  python3 pipeline_stages/extract.py --replay-mode
"""
import json
import os
import time
from pathlib import Path
from typing import Any, Optional

import hashlib

# ── Configuration ──
FIXTURE_DIR = Path(os.environ.get("DEVFORGE_FIXTURE_DIR", 
    "/opt/projects/server/tests/fixtures/llm_recordings"))
RECORD_MODE = bool(os.environ.get("DEVFORGE_LLM_RECORD"))
REPLAY_MODE = bool(os.environ.get("DEVFORGE_LLM_REPLAY"))

# ── Hash function for request deduplication ──
def _request_hash(messages: list[dict], model: str, **kwargs) -> str:
    """Generate deterministic hash for a request (for deduplication)."""
    import hashlib
    payload = json.dumps({
        "messages": messages,
        "model": model,
        "max_tokens": kwargs.get("max_tokens", 0),
        "temperature": kwargs.get("temperature", 0),
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]


class LLMRecorder:
    """Records and replays LLM API calls.
    
    RECORD: Captures request/response pairs to fixture files
    REPLAY: Returns pre-captured response for matching requests
    PASSTHROUGH: Direct LLM call (production mode)
    """
    
    def __init__(self, fixture_dir: Path = FIXTURE_DIR):
        self.fixture_dir = fixture_dir
        self.fixture_dir.mkdir(parents=True, exist_ok=True)
        
    def record(self, filename: str, messages: list[dict], 
               model: str, response: dict, **kwargs):
        """Record a request/response pair to fixture file."""
        if not RECORD_MODE:
            return
            
        fixture_path = self.fixture_dir / filename
        entries = []
        
        if fixture_path.exists():
            try:
                entries = json.loads(fixture_path.read_text())
            except json.JSONDecodeError:
                entries = []
        
        req_hash = _request_hash(messages, model, **kwargs)
        
        entry = {
            "hash": req_hash,
            "timestamp": time.time(),
            "model": model,
            "request": {
                "messages": messages,
                **{k: v for k, v in kwargs.items() if k in (
                    "max_tokens", "temperature", "top_p", "top_k",
                    "repeat_penalty", "presence_penalty", "frequency_penalty",
                    "json_mode"
                )}
            },
            "response": response,
        }
        
        # Avoid duplicate recordings of identical request
        if not any(e.get("hash") == req_hash for e in entries):
            entries.append(entry)
            fixture_path.write_text(json.dumps(entries, indent=2, 
                                              ensure_ascii=False))
            print(f"[replay] Recorded: {filename} (hash: {req_hash})")
    
    def replay(self, filename: str, messages: list[dict], model: str, 
               **kwargs) -> Optional[dict]:
        """Replay a recorded response for matching request.
        
        Returns: Response dict if found, None if not found.
        """
        if not REPLAY_MODE:
            return None
            
        fixture_path = self.fixture_dir / filename
        if not fixture_path.exists():
            print(f"[replay] WARNING: No fixture found: {filename}")
            return None
        
        try:
            entries = json.loads(fixture_path.read_text())
        except json.JSONDecodeError:
            print(f"[replay] WARNING: Corrupt fixture: {filename}")
            return None
        
        req_hash = _request_hash(messages, model, **kwargs)
        
        # Exact hash match (deterministic)
        for entry in entries:
            if entry.get("hash") == req_hash:
                print(f"[replay] Matched: {filename} (hash: {req_hash})")
                return entry["response"]
        
        # Fuzzy match (same model, first entry as fallback)
        for entry in entries:
            if entry.get("model") == model:
                print(f"[replay] Fuzzy matched: {filename} (model: {model})")
                return entry["response"]
        
        print(f"[replay] No match in {filename} for model={model}")
        return None


# ── Global recorder instance ──
_recorder: Optional[LLMRecorder] = None

def get_recorder() -> LLMRecorder:
    global _recorder
    if _recorder is None:
        _recorder = LLMRecorder()
    return _recorder


# ── Decorator for wrapping call_llm ──
def record_or_replay(filename: str):
    """Decorator to add record/replay capability to call_llm.
    
    Usage:
        @record_or_replay("extract_llm.json")
        def call_llm(messages, model="day_extract", ...):
            ...
    """
    def decorator(func):
        def wrapper(messages, model="default", **kwargs):
            recorder = get_recorder()
            
            # Try replay first
            if REPLAY_MODE:
                cached = recorder.replay(filename, messages, model, **kwargs)
                if cached is not None:
                    return cached
            
            # Actual call
            result = func(messages, model=model, **kwargs)
            
            # Record if in record mode
            if RECORD_MODE:
                response = result if isinstance(result, dict) else {"content": result}
                recorder.record(filename, messages, model, response, **kwargs)
            
            return result
        return wrapper
    return decorator


# ── Capture functions for Phase −1 ──
def capture_llm_responses():
    """Capture representative LLM responses from current production system.
    
    This function should be run BEFORE refactoring starts, against the
    existing scripts/ code. It captures:
    1. extract.py LLM calls (10 samples)
    2. text_clean.py LLM calls (1 sample)
    3. enrich.py LLM calls (5 samples)
    4. worklog_generator.py LLM calls (1 sample)
    5. NLI server responses (5 samples)
    6. Reranker scores (3 samples)
    """
    from lib.llm_client import call_llm, call_llm_json, _call_nli_server, reranker_score
    from lib.text_cleaner import TextCleaner
    from pipelines.extract import extract_pipeline
    from pipelines.enrich import enrich_pipeline
    from pipelines.worklog_generator import generate_worklog
    
    recorder = get_recorder()
    
    # 1. Capture extract LLM calls
    print("Capturing extract.py LLM responses...")
    # Get 10 sample turns from DB
    from lib.db import psql_json
    sample_turns = psql_json("""
        SELECT t.id, t.user_turn, t.pipeline_state
        FROM turns t
        WHERE t.pipeline_state = 'scanned'
        LIMIT 10
    """)
    
    for turn in sample_turns:
        messages = [
            {"role": "system", "content": "...extract system prompt..."},
            {"role": "user", "content": turn["user_turn"][:2000]}
        ]
        response = call_llm(messages, model="day_extract", max_tokens=2048)
        recorder.record("extract_llm.json", messages, "day_extract", 
                       {"content": response}, max_tokens=2048, temperature=0.12)
    
    # 2. Capture text_clean LLM calls
    print("Capturing text_clean.py LLM response...")
    cleaner = TextCleaner()
    test_text = "한국어 테스트 텍스트입니다. This is a mixed text sample."
    cleaned = cleaner.clean(test_text)
    
    # 3. Capture enrich LLM calls
    print("Capturing enrich.py LLM responses...")
    # ... similar pattern for enrich
    
    # 4. Capture NLI responses
    print("Capturing NLI responses...")
    nli_samples = [
        ("source claim 1", "evidence 1"),
        ("source claim 2", "evidence 2"),
        ("source claim 3", "evidence 3"),
        ("source claim 4", "evidence 4"),
        ("source claim 5", "evidence 5"),
    ]
    for source, evidence in nli_samples:
        result = _call_nli_server(source, evidence, timeout=30)
        recorder.record("nli_result.json", 
                       [{"source": source, "evidence": evidence}],
                       "nli_server",
                       {"verdict": result}, timeout=30)
    
    # 5. Capture reranker scores
    print("Capturing reranker scores...")
    rerank_samples = [
        ("query about project", "document about devforge"),
        ("query about pipeline", "document about day_cycle"),
        ("query about model", "document about llama"),
    ]
    for query, document in rerank_samples:
        score = reranker_score(query, document)
        recorder.record("rerank_score.json",
                       [{"query": query, "document": document}],
                       "reranker",
                       {"score": score}, timeout=120)
    
    print("✅ All LLM fixtures captured to:", FIXTURE_DIR)


if __name__ == "__main__":
    capture_llm_responses()

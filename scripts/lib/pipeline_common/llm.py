#!/usr/bin/env python3
# Status: production
"""LLM call helpers for pipeline use."""

import json

from lib.common import log
from lib.llm.json_parser import parse_llm_json as _extract_json
from lib.llm.json_parser import save_dlq
from lib.llm_client import call_llm
from lib.pipeline_common.helpers import abort, strip_code_fence
from lib.pipeline_common.schema import _log_schema_warnings

# Default timeout for LLM calls (seconds, aligned with pod_manager.TIMEOUT)
TIMEOUT = 7200


def llm_call(messages, model, max_tokens=2048, label=""):
    raw_content = ""

    def _try(m):
        nonlocal _raw
        r = call_llm(
            messages,
            model=model,
            max_tokens=max_tokens,
            timeout=TIMEOUT,
            json_mode=m,
            return_meta=True,
        )
        content = r["content"]
        raw_content = content
        _raw = raw_content
        if isinstance(content, str):
            content = strip_code_fence(content)
        return _extract_json(content), r, raw_content

    _raw = ""
    try:
        result, r, raw_content = _try(False)
        _log_schema_warnings(result, label, model)
        return {
            "result": result,
            "usage": r.get("usage", {}),
            "timings": r.get("timings", {}),
            "elapsed_ms": r.get("elapsed_ms", 0),
        }
    except json.JSONDecodeError as e:
        save_dlq(_raw, stage=label, model=model, error=str(e), attempt=1)
        log(f"  JSON parse error {label}: {e}. Self-correcting...")
        if not _raw:
            abort("LLM JSON parsing failed (raw response empty)", label, str(e))
        try:
            correct_msgs = [
                {
                    "role": "system",
                    "content": "Convert the following text into valid JSON. Return ONLY the JSON, no markdown.",
                },
                {"role": "user", "content": f"Convert this to valid JSON:\n\n{_raw[:3000]}"},
            ]
            r = call_llm(
                correct_msgs,
                model=model,
                max_tokens=max_tokens,
                timeout=TIMEOUT,
                json_mode=False,
                return_meta=True,
            )
            content = strip_code_fence(r["content"])
            result = _extract_json(content)
            _log_schema_warnings(result, label + "_corrected", model)
            return {
                "result": result,
                "usage": r.get("usage", {}),
                "timings": r.get("timings", {}),
                "elapsed_ms": r.get("elapsed_ms", 0),
            }
        except Exception as e2:
            save_dlq(_raw, stage=label + "_corrected", model=model, error=str(e2), attempt=2)
            abort("LLM call failed (self-correction also failed)", label, str(e2))
            return None
    except Exception as e:
        log(f"  ERROR {label}: {e}")
        abort("LLM call failed", label, str(e))
        return None

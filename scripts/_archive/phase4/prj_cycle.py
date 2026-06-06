#!/usr/bin/env python3
"""
P-R-J 고정 역할 실험: P=30B, R=Qwen14B, J=Selene Q8

P-R-J = 30B + Qwen2.5-Coder-14B + Codestral-22B (고정 역할)
P(30B) -> R(Qwen14B) -> J(Codestral-22B) 1회 패스

파이프라인: Python 검증 -> 30B verify -> P-R-J 1회 -> 핸드오프 저장

메모리 관리: phase 전환마다 podman stop로 모든 컨테이너 완전 제거 -> 필요한 것만 시작
Pod B에서 순차 swap (30B -> Qwen14B -> Codestral-22B)

사용법:
  python3 prj_cycle.py
"""

import json, os, subprocess, sys, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

RESUME_PRJ = "--resume-prj" in sys.argv
DRY_RUN = "--dry-run" in sys.argv
INPUT_OVERRIDE = None
for _i, _a in enumerate(sys.argv):
    if _a == "--input" and _i + 1 < len(sys.argv):
        INPUT_OVERRIDE = sys.argv[_i + 1]

if DRY_RUN:
    print("[DRY RUN] 모드 활성화 — LLM 호출/컨테이너 없이 데이터 흐름만 검증")
    print()

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
EXPER_DIR = os.path.join(SCRIPTS_DIR, "..", "data", "experiment")
os.makedirs(EXPER_DIR, exist_ok=True)
sys.path.insert(0, SCRIPTS_DIR)
from lib.llm_client import call_llm
from lib.db import psql, psql_ok, esc_sql

MODE_FILE_B = "/opt/ai_data/scripts/current-mode-pod-b.env"
MODE_FILE_A = "/opt/ai_data/scripts/current-mode-pod-a.env"
TIMEOUT = 7200

PASS = "[PASS]"
FAIL = "[FAIL]"
WARN = "[WARN]"

# ── Slack notification ──────────────────────────────────────────

_SF = Path.home() / ".config/devforge/secrets.env"
_SLACK_TOKEN = ""
_SLACK_CHANNEL = "U0APJGD8CBW"
if _SF.exists():
    for _line in _SF.read_text().split("\n"):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            if _k.strip() == "SLACK_BOT_TOKEN":
                _SLACK_TOKEN = _v.strip().strip('"').strip("'")
            elif _k.strip() == "SLACK_CHANNEL":
                _SLACK_CHANNEL = _v.strip().strip('"').strip("'")


def slack_send(text):
    """실험 진행상황을 Slack DM으로 전송."""
    if not _SLACK_TOKEN:
        return
    payload = json.dumps({"channel": _SLACK_CHANNEL, "text": text, "mrkdwn": True}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage", data=payload,
        headers={"Authorization": f"Bearer {_SLACK_TOKEN}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if not result.get("ok"):
                log(f"Slack API error: {result.get('error', '?')}")
    except Exception as e:
        log(f"Slack send failed: {e}")


def ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def log(msg):
    print(f"[{ts()}] {msg}", flush=True)


def load_input():
    if INPUT_OVERRIDE:
        fpath = INPUT_OVERRIDE
        if not os.path.isabs(fpath):
            fpath = os.path.join(SCRIPTS_DIR, "..", fpath)
    else:
        fpath = os.path.join(SCRIPTS_DIR, "..", "pipeline_input", "consolidated_input_compact.json")
        if not os.path.exists(fpath):
            fpath = fpath.replace("_compact", "")
    with open(fpath) as f:
        return json.load(f)


def wait_health(port, timeout=600):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
            with urllib.request.urlopen(req, timeout=3) as r:
                if r.status == 200:
                    return True
        except: pass
        time.sleep(3)
    return False


def wait_probe(port, model_name, timeout=300):
    """헬스 OK 후 모델이 실제 추론 가능할 때까지 probe 요청 전송.

    --no-warmup 모델은 health endpoint가 OK를 리턴해도 모델이
    아직 로딩 중일 수 있음. 작은 추론 요청을 보내 정상 응답을 확인."""
    t0 = time.monotonic()
    body = json.dumps({
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 5, "temperature": 0.1, "stream": False,
    }).encode()
    while time.monotonic() - t0 < timeout:
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
                if data.get("choices") and data["choices"][0].get("message"):
                    log(f"  probe OK ({model_name})")
                    return True
        except: pass
        time.sleep(5)
    log(f"  probe TIMEOUT ({model_name})")
    return False


# ── Container management: kill all -> start only what's needed ──────────
# phase 전환마다 모든 컨테이너를 내리고 새로 시작해야 하는 것만 시작


def kill_all():
    """모든 systemd container 서비스 정지. 메모리 완전 확보.

    systemctl stop을 사용해야 systemd의 Restart=on-failure가 트리거되지 않음.
    podman stop을 쓰면 entrypoint가 exit 1로 종료되어 systemd가 60초 후
    의도치 않게 컨테이너를 재시작해 phase 간 OOM을 유발함."""
    if DRY_RUN:
        log("  [DRY] kill_all() skipped")
        return
    log("  systemctl stop containers...")
    subprocess.run(["systemctl", "--user", "stop", "container-devforge-swap.service"],
                   capture_output=True, timeout=30)
    subprocess.run(["systemctl", "--user", "stop", "container-devforge-qwen.service"],
                   capture_output=True, timeout=30)
    subprocess.run(["systemctl", "--user", "reset-failed", "container-devforge-swap.service"],
                   capture_output=True, timeout=10)
    subprocess.run(["systemctl", "--user", "reset-failed", "container-devforge-qwen.service"],
                   capture_output=True, timeout=10)
    # 커널 메모리 회수 시간 확보.
    # 컨테이너가 종료된 후 cgroup/pasta가 port를 해제하고,
    # 커널이 page cache/slab을 회수하는 데 시간이 필요함 (특히 16GB+ 모델 전환 시).
    _reclaim_memory()


def _reclaim_memory():
    """모든 컨테이너 정지 후 커널 메모리 회수를 위한 충분한 대기.

    kill_all() 직후 호출됨. 다음 동작 수행:
    1. sync() — dirty page cache를 디스크에 플러시
    2. 15초 sleep — 커널이 page cache, dentry, inode cache 회수
    3. 추가 5초 — cgroup/pasta 네트워크 스택 해제 대기

    총 20초 대기. 16GB+ 모델 전환 시 OOM 방지에 필수.
    """
    import os as _os
    _os.sync()
    log("  Memory reclaim: synced fs, waiting 15s for kernel reclaim...")
    time.sleep(15)
    log("  Memory reclaim: done")


def start_pod_b(mode, port):
    """Pod B만 시작 (먼저 kill_all로 기존 컨테이너 제거)."""
    log(f"  POD B -> {mode} (:{port})")
    with open(MODE_FILE_B, "w") as f:
        f.write(f"MODE={mode}")
    kill_all()
    subprocess.run(["systemctl", "--user", "start", "container-devforge-swap.service"],
                   capture_output=True, timeout=60)
    ok = wait_health(port)
    if ok:
        log(f"  :{port} health OK")
        ok = wait_probe(port, mode, timeout=600)
    if ok:
        log(f"  :{port} ready")
        time.sleep(5)  # tiny buffer after health OK
    return ok


def start_pod_a(mode, port):
    """Pod A만 시작 (먼저 kill_all로 기존 컨테이너 제거)."""
    log(f"  POD A -> {mode} (:{port})")
    with open(MODE_FILE_A, "w") as f:
        f.write(f"MODE={mode}")
    kill_all()
    subprocess.run(["systemctl", "--user", "start", "container-devforge-qwen.service"],
                   capture_output=True, timeout=60)
    ok = wait_health(port)
    if ok:
        log(f"  :{port} health OK")
        ok = wait_probe(port, mode, timeout=600)
    else:  log(f"  :{port} TIMEOUT (Pod A {mode})")
    return ok


def start_day_both():
    """Day 모드 = Pod A 3B Q8_0(:8082) 먼저 시작 -> Pod B 7B Q8_0(:8080) 순차 시작.
    3B(3.1GB) + 7B(7.6GB) = ~11GB — 순차 로딩으로 RAM 경합 방지."""
    log("  DAY MODE: Pod A 3B -> Pod B 7B (순차)")
    with open(MODE_FILE_B, "w") as f:
        f.write("MODE=day")
    with open(MODE_FILE_A, "w") as f:
        f.write("MODE=day")
    kill_all()
    # Pod A 먼저 (가벼운 모델, 빠름)
    subprocess.run(["systemctl", "--user", "start", "container-devforge-qwen.service"],
                   capture_output=True, timeout=60)
    ok_a = wait_health(8082)
    if ok_a: log(f"  :8082 ready (Pod A 3B)")
    else:    log(f"  :8082 TIMEOUT (Pod A 3B)")

    # Pod B 7B는 Pod A가 안정화된 후 시작
    subprocess.run(["systemctl", "--user", "start", "container-devforge-swap.service"],
                   capture_output=True, timeout=60)
    ok_b = wait_health(8080)
    if ok_b: log(f"  :8080 ready (7B)")
    else:    log(f"  :8080 TIMEOUT (7B)")
    return ok_a and ok_b


def ensure_model(model_name):
    """단일 모델만 띄움. 이전 모든 컨테이너는 kill_all로 제거."""
    if DRY_RUN:
        log(f"  [DRY] ensure_model({model_name}) → OK (mock)")
        return True
    if model_name == "Qwen30B":
        return start_pod_b("review-p", 8080)
    if model_name == "Qwen14B":
        return start_pod_b("review-r", 8080)
    if model_name == "Qwen7B":
        return start_pod_b("test-7b", 8080)
    if model_name == "Codestral":
        return start_pod_b("review-j", 8080)
    if model_name == "Qwen27B":
        return start_pod_b("verify", 8081)
    log(f"  Unknown model: {model_name}")
    return False


# ── Pipeline State Blackboard ──────────────────────────────────────────
# 단일 JSON 파일에 모든 phase 결과를 축적. 각 phase는 add_phase()로 추가,
# build_context()로 다음 phase의 LLM 프롬프트용 요약문 생성.


class PipelineState:
    """Blackboard: append-only phase results in 1 JSON file.
    add_phase(key, value) → saves to pipeline_state_{tag}.json
    build_context(phase) → reads accumulated state, returns structured text for LLM prompt."""

    def __init__(self, round_num, with_rubric, input_data, existing_data=None):
        self.round_num = round_num
        self.with_rubric = with_rubric
        self.tag = f"r{round_num}_{'rubric' if with_rubric else 'norubric'}"
        self.path = os.path.join(EXPER_DIR, f"pipeline_state_{self.tag}.json")

        if existing_data:
            self.data = existing_data
            self.save()
            return

        findings = input_data.get("findings", [])

        sev = {}
        src_files = {}
        for f in findings:
            s = f.get("severity", "unknown").lower()
            sev[s] = sev.get(s, 0) + 1
            sf = f.get("source_file", "unknown")
            src_files[sf] = src_files.get(sf, 0) + 1

        # ── 3B Extract test results (from consolidated input) ──
        ext_input = input_data.get("extract", {})
        extract_models = ext_input.get("models", [])

        self.data = {
            "meta": {"round": round_num, "with_rubric": with_rubric,
                     "created_at": datetime.now(timezone.utc).isoformat()},
            "extract": {"models": extract_models,
                        "total_models": len(extract_models),
                        "description": input_data.get("description", ""),
                        "total_files_merged": input_data.get("total_files_merged", 0)},
            "input": {"total_findings": len(findings),
                      "severity_distribution": sev,
                      "source_files": src_files,
                      "findings": findings},
            "python_verify": {},
            "30b_verify": {},
            "rubric_evaluation": {},
            "prj": [],
            "handoffs": [],
            "final_verify": {},
        }
        self.save()

    def save(self):
        os.makedirs(EXPER_DIR, exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def add_phase(self, key, value):
        self.data[key] = value
        self.save()

    def add_prj_rotation(self, rotation_result):
        self.data["prj"].append(rotation_result)
        self.save()

    def build_context(self, phase, extra=None):
        """LLM 프롬프트용 컨텍스트 생성.
        phase 컨트롤스 inclusion: 'prj_p'는 30B 결과 포함, '30b_verify'는 python_verify만."""
        parts = []
        inp = self.data["input"]
        findings = inp.get("findings", [])

        # Header
        rl = f"Round {self.round_num}" + (" (with rubric)" if self.with_rubric else "")
        parts.append(f"=== PIPELINE CONTEXT: {rl} ===\n")

        # ── 3B Extract summary ──
        ext = self.data.get("extract", {})
        extract_models = ext.get("models", [])
        if extract_models:
            parts.append(f"[3B EXTRACT] {len(extract_models)} models x 15 turns")
            for m in extract_models:
                fth = f"{m.get('faithfulness_rate',0)*100:.0f}%"
                sec = m.get('elapsed_seconds', 0)
                ext_count = m.get('total_extractions', 0)
                parts.append(f"  {m['model']}: faith={fth} ({m.get('total_faithful',0)}/{ext_count}), "
                             f"time={sec:.0f}s ({m.get('avg_time_per_turn','?')}s/turn), ok={m.get('turns_ok',0)}/{m.get('turns_total',0)}")
            parts.append("")

        # Input summary
        sev = inp["severity_distribution"]
        parts.append(f"[INPUT] {inp['total_findings']} findings ({', '.join(f'{k}={v}' for k,v in sorted(sev.items()) if v > 0)})")
        srcs = inp["source_files"]
        parts.append(f"  Sources: {', '.join(s.split('/')[-1]+'='+str(c) for s,c in sorted(srcs.items()))}\n")

        # Python verify (skip for the python_verify phase itself)
        pv = self.data.get("python_verify")
        if pv and pv.get("total_findings") and phase not in ("python_verify",):
            status = "PASS" if pv.get("issues_found", 0) == 0 else f"{pv['issues_found']} ISSUES"
            parts.append(f"[PYTHON VERIFY] {status}")
            for iss in pv.get("issues", [])[:3]:
                parts.append(f"  - {iss['check']}: {iss.get('detail','')[:100]}")
            parts.append("")

        # 30B verify (skip for python_verify and 30b_verify phases)
        v30 = self.data.get("30b_verify")
        if v30 and v30.get("final_verdict") and phase not in ("python_verify", "30b_verify"):
            parts.append(f"[30B VERIFY] {v30['final_verdict']} (confidence={v30.get('confidence','?')})")
            for item in v30.get("verification_items", [])[:5]:
                parts.append(f"  [{item.get('result','?')}] {item.get('check','')}")
            rsn = v30.get("reasoning", "")
            if rsn:
                parts.append(f"  Reasoning: {rsn[:200]}")
            parts.append("")

        # P-R-J results (skip for early phases and prj phases themselves)
        prj = self.data.get("prj", [])
        if prj and phase not in ("python_verify", "30b_verify", "prj_p", "prj_r", "prj_j"):
            parts.append(f"[P-R-J] {len(prj)} rotations:")
            for r in prj:
                parts.append(f"  {r.get('rotation','?')}: P={r.get('p_model','?')}({r.get('P_score','?')}) R={r.get('r_model','?')}({r.get('R_score','?')}) J={r.get('j_model','?')} → score={r.get('consensus','?')} {r.get('decision','?')}")
            parts.append("")

        # ── Findings details (all phases except python_verify) ──
        if phase != "python_verify":
            parts.append("[FINDINGS BY SEVERITY]")
            for sev_name in ("critical", "high", "medium", "low", "partial", "fail"):
                f_list = [f for f in findings if f.get("severity", "").lower() == sev_name]
                if not f_list:
                    continue
                parts.append(f"\n[{sev_name.upper()}] ({len(f_list)}):")
                for f in f_list[:5]:
                    fid = f.get("fid", f.get("id", "?"))
                    desc = f.get("description", "").replace("\n", " ")[:120]
                    parts.append(f"  {fid}: {desc}")
                if len(f_list) > 5:
                    parts.append(f"  ... +{len(f_list)-5} more")

            # 30B verification items → Proposer가 중복 회피 (only for P)
            if phase == "prj_p" and v30:
                items = v30.get("verification_items", [])
                if items:
                    parts.append(f"\n[30B ALREADY REVIEWED — do not re-review these]")
                    for item in items[:8]:
                        parts.append(f"  [{item.get('result','?')}] {item.get('check','')}: {item.get('detail','')[:100]}")

        # ── Phase-specific ──

        # Rubric evaluation context (for P, J, and final_verify)
        rub = self.data.get("rubric_evaluation", {})
        rub_evals = rub.get("evaluations", [])
        if rub_evals and phase in ("prj_p", "prj_j", "final_verify"):
            parts.append("\n[RUBRIC EVALUATION — finding-level scores]")
            low_scorers = [r for r in rub_evals if r.get("weighted_score", 10) < 5.0]
            for r in rub_evals[:10]:
                fid = r.get("id", "?")
                ws = r.get("weighted_score", 0)
                c = r.get("correctness", 0)
                a = r.get("actionability", 0)
                e = r.get("evidence", 0)
                n = r.get("novelty", 0)
                parts.append(f"  {fid}: weighted={ws:.1f} C={c} A={a} E={e} N={n}")
            if low_scorers:
                parts.append(f"  LOW SCORERS (<5.0): {len(low_scorers)} findings — prioritize review")
            parts.append("")

        if phase == "prj_j":
            # Judge: which rotation, previous rotation context
            ri = (extra or {}).get("rotation_index", 0)
            parts.append(f"[JUDGE ROTATION {ri+1}/3]")
            if ri > 0 and len(prj) > 0:
                prev = prj[-1]
                parts.append(f"  Previous: {prev.get('rotation','?')} consensus={prev.get('consensus','?')} decision={prev.get('decision','?')}")
                if prev.get("report_summary"):
                    parts.append(f"  Summary: {prev['report_summary'][:150]}")

        elif phase == "final_verify":
            parts.append(f"\n[FINAL VERIFY] 7 handoff documents below (3 LLM-R + 3 Python + 1 consolidated)")

        return "\n".join(parts)


def strip_code_fence(text):
    """LLM이 JSON을 ```json ... ```로 감싸서 반환할 경우 벗겨냄.

    R1-8B(DeepSeek-R1 distilled)는 response_format=json_object를
    무시하고 마크다운 코드 블록으로 감싸서 응답하는 경우가 있음."""
    text = text.strip()
    if text.startswith("```"):
        # ```json ... ``` 또는 ``` ... ``` 형태 제거
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1:]
        if text.endswith("```"):
            text = text[:-3].rstrip()
        elif text.endswith("``"):
            text = text[:-2].rstrip()
    return text.strip()


def _extract_json(text):
    """JSON-like 문자열을 텍스트에서 추출. ```json 블록, 순수 JSON, 텍스트 내 JSON 순으로 시도."""
    s = text.strip()
    # 1) ```json ... ``` 또는 ``` ... ```
    if s.startswith("```"):
        first_nl = s.find("\n")
        if first_nl != -1:
            s = s[first_nl + 1:]
        if s.endswith("```"):
            s = s[:-3].rstrip()
        elif s.endswith("``"):
            s = s[:-2].rstrip()
        return json.loads(s)
    # 2) 순수 JSON
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # 3) 텍스트에서 {...} 또는 [...] 찾기
    for brace in ("{", "["):
        start = s.find(brace)
        if start == -1:
            continue
        end = s.rfind("}" if brace == "{" else "]")
        if end == -1 or end <= start:
            continue
        try:
            return json.loads(s[start:end + 1])
        except json.JSONDecodeError:
            pass
    raise json.JSONDecodeError(f"JSON not found in response", s, 0)


def llm_call(messages, model, max_tokens=2048, label=""):
    """LLM 호출 → JSON 파싱.

    json_mode=True를 지원하지 않는 모델(Mistral계열 Codestral 등)은 system prompt로 JSON을 유도하고
    응답에서 _extract_json()으로 JSON을 추출한다. R1-8B는 json_mode 유지 + reasoning 소진시
    max_tokens*2 재시도.
    """
    def _try(m):
        """단일 시도: m=True/False에 따라 json_mode on/off. returns (result, raw_r, raw_content)."""
        nonlocal _raw
        r = call_llm(messages, model=model, max_tokens=max_tokens,
                     timeout=TIMEOUT, json_mode=m, return_meta=True)
        content = r["content"]
        raw_content = content
        _raw = raw_content  # save for self-correction fallback
        if isinstance(content, str):
            content = strip_code_fence(content)
        return _extract_json(content), r, raw_content

    _raw = ""  # raw response for self-correction fallback
    try:
        # R1-8B: json_mode 유지, reasoning 소진시 2x 토큰 재시도
        if model == "R1-8B":
            try:
                result, r, _ = _try(True)
            except json.JSONDecodeError:
                log(f"  R1-8B empty content (reasoning consumed all {max_tokens} tokens). Retrying with {max_tokens*2} tokens...")
                _saved_max = max_tokens
                max_tokens = max_tokens * 2
                result, r, _ = _try(True)
                max_tokens = _saved_max
            return {
                "result": result, "usage": r.get("usage", {}),
                "timings": r.get("timings", {}), "elapsed_ms": r.get("elapsed_ms", 0),
            }

        # Codestral/비Qwen: json_mode 지원 안 함 → 바로 non-json_mode 호출
        result, r, _raw = _try(False)
        return {
            "result": result, "usage": r.get("usage", {}),
            "timings": r.get("timings", {}), "elapsed_ms": r.get("elapsed_ms", 0),
        }

    except json.JSONDecodeError as e:
        # 실패시 self-correction: raw 응답을 JSON으로 변환하도록 재요청
        log(f"  JSON parse error {label}: {e}. Self-correcting...")
        if not _raw:
            abort(f"LLM JSON 파싱 실패 (raw 응답 없음)", label, str(e))
        try:
            correct_msgs = [
                {"role": "system", "content": "Convert the following text into valid JSON. Return ONLY the JSON, no markdown."},
                {"role": "user", "content": f"Convert this to valid JSON:\n\n{_raw[:3000]}"},
            ]
            r = call_llm(correct_msgs, model=model, max_tokens=max_tokens,
                         timeout=TIMEOUT, json_mode=False, return_meta=True)
            content = strip_code_fence(r["content"])
            result = _extract_json(content)
            return {
                "result": result, "usage": r.get("usage", {}),
                "timings": r.get("timings", {}), "elapsed_ms": r.get("elapsed_ms", 0),
            }
        except Exception as e2:
            abort(f"LLM 호출 실패 (self-correction도 실패)", label, str(e2))
            return None
    except Exception as e:
        log(f"  ERROR {label}: {e}")
        abort(f"LLM 호출 실패", label, str(e))
        return None  # unreachable


def abort(phase, label, detail):
    """실패 시 파이프라인 중단, Slack 알림 전송, exit."""
    log(f"\n{'='*60}")
    log(f"!! ABORT: {phase} — {label}")
    log(f"!! Detail: {detail}")
    log(f"{'='*60}")
    slack_send(
        f":no_entry: *P-R-J 실험 중단* — {phase}\n"
        f"> {label}: {detail[:200]}"
    )
    sys.exit(1)


def save(phase, tag, data):
    fname = f"exp_{phase}_{tag}.json"
    fpath = os.path.join(EXPER_DIR, fname)
    with open(fpath, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return fpath


# ── System prompts ─────────────────────────────────────────────────────

SYS_P = """You are a code review specialist. Analyze the evaluation findings below. Identify bugs, security issues, data loss risks, and edge cases.

When P, R, J agree: if consensus within 1 round → inject STRONG dissent.
If disagreement < 30% among findings → inject MODERATE dissent.
Otherwise proceed — consensus is genuine.

=== EVALUATION RUBRIC (self-assessment) ===
Rate your OWN findings on these criteria:
- Correctness (0-5): 0=wrong, 3=mostly correct, 5=fully correct
- Actionability (0-5): 0=no fix, 3=partial fix, 5=clear fix
- Evidence (0-5): 0=no evidence, 3=partial citation, 5=exact file:line
- Novelty (0-5): 0=duplicate, 3=somewhat new, 5=unique insight

Return JSON:
{
  "findings": [
    {"id": "F001", "severity": "critical|high|medium|low", "category": "bug|security|data_loss|performance|quality", "description": "1-3 sentence explanation", "file": "filename or area", "evidence": {"file": "path/to/file", "line": "42-57", "quote": "exact code or text"}}
  ],
  "rubric_evaluation": {
    "correctness": 0-5,
    "correctness_justification": "why this score",
    "actionability": 0-5,
    "actionability_justification": "...",
    "evidence": 0-5,
    "evidence_justification": "...",
    "novelty": 0-5,
    "novelty_justification": "..."
  }
}"""

SYS_R = """You are a review reflector. For each finding submitted by the Proposer, decide ACCEPT or REJECT. Be precise — if the finding is valid, ACCEPT it. If it is not a real issue or duplicates another, REJECT it.

=== EVALUATION RUBRIC (self-assessment) ===
Rate your OWN verdicts on these criteria:
- Accuracy (0-5): 0=wrong, 3=mostly right, 5=fully correct
- Reasoning (0-5): 0=vague, 3=adequate, 5=precise & specific
- Efficiency (0-5): 0=verbose, 3=reasonable, 5=concise

Return JSON:
{
  "verdicts": [
    {"id": "F001", "verdict": "accept", "reason": "concise justification", "evidence": {"finding_id": "F001", "source_check": "file:line"}},
    {"id": "F002", "verdict": "reject", "reason": "concise justification", "evidence": {"finding_id": "F002", "source_check": "file:line"}}
  ],
  "rubric_evaluation": {
    "accuracy": 0-5,
    "accuracy_justification": "why this score",
    "reasoning": 0-5,
    "reasoning_justification": "...",
    "efficiency": 0-5,
    "efficiency_justification": "..."
  }
}"""

SYS_J = """You are a Scoring Judge evaluating both the Proposer (P) and Reflector (R).

P_score = Correctness(0-5) + Coverage(0-5) + Precision(0-5) -> 0-15
R_score = Accuracy(0-5) + Efficiency(0-5) + Completeness(0-5) -> 0-15
J_score = Fairness(0-5) + Consistency(0-5) + Clarity(0-5) -> 0-15

=== EVALUATION RUBRIC (self-assessment) ===
Rate your OWN judging:
- Fairness (0-5): 0=biased, 3=fair, 5=perfectly balanced with evidence
- Consistency (0-5): 0=contradictory, 3=mostly aligned, 5=fully consistent
- Clarity (0-5): 0=unclear, 3=adequate, 5=crystal clear with specific examples

IMPORTANT — handoff rules:
- The "handoff" fields below must be derived ONLY from the actual approved/rejected
  decisions you just made (listed in "approved" and "rejected" arrays above).
- Do NOT add finding IDs that are not in your approved/rejected arrays.
- Do NOT fabricate or guess finding content.
- "unresolved_count" = len(rejected) — findings rejected by R are unresolved.
- "critical_remaining" = IDs of rejected findings that had severity "critical" or "high".
- "key_accepted"/"key_rejected" = first 5 of each, already in the arrays above.

Return ONLY valid JSON — no markdown, no commentary.

Return JSON:
{
  "P_score": 0-15,
  "P_rubric": {"correctness": 0-5, "coverage": 0-5, "precision": 0-5},
  "P_evidence": {"correctness": [], "coverage": [], "precision": []},
  "R_score": 0-15,
  "R_rubric": {"accuracy": 0-5, "efficiency": 0-5, "completeness": 0-5},
  "R_evidence": {"accuracy": [], "efficiency": [], "completeness": []},
  "J_score": 0-15,
  "J_rubric": {"fairness": 0-5, "consistency": 0-5, "clarity": 0-5},
  "J_evidence": {"fairness": [], "consistency": [], "clarity": []},
  "decision": "APPROVED|REJECT",
  "consensus_score": 0-100,
  "approved": ["F001"],
  "rejected": [],
  "decisions": [{"id": "F001", "decision": "approved|rejected", "reason": "..."}],
  "rubric_evaluation": {
    "fairness": 0-5,
    "fairness_justification": "...",
    "clarity": 0-5,
    "clarity_justification": "...",
    "consistency": 0-5,
    "consistency_justification": "..."
  },
  "report": {
    "summary": "1-2 sentence overall assessment of this rotation",
    "top_issues": ["most critical finding in 1 line"],
    "quality_notes": {"strengths": ["..."], "weaknesses": ["..."]},
    "recommendation": "commit or escalate in 1 sentence"
  },
  "handoff": {
    "rotation_summary": "Brief state of findings after this rotation",
    "unresolved_count": <number>,
    "critical_remaining": ["F001"],
    "verifier_focus": ["area for verifier to double-check"],
    "key_accepted": ["F001"],
    "key_rejected": ["F002"]
  }
}"""

SYS_V27 = """You are a final verifier. Review all findings and P-R-J results.

You will receive THREE handoff documents:
1. [LLM-R] — R(14B) handoff (comprehensive summary after full P-R-J cycle)
2. [Python] — deterministic handoff
3. [Python consolidated] — full rotation summary

Compare LLM-R vs Python. After your final verdict,
write detailed, actionable feedback per model+role:
e.g., P=Qwen30B, R=Qwen14B, J=Selene — separate feedback for each.

=== EVALUATION RUBRIC (self-assessment) ===
Rate your OWN verification on these criteria:
- Thoroughness (0-5): 0=skipped, 3=partial, 5=all docs cross-checked
- Evidence Check (0-5): 0=no data, 3=some, 5=all items backed by specific data
- Feedback Quality (0-5): 0=vague, 3=adequate, 5=actionable per-role feedback

Return JSON:
{
  "final_verdict": "approved|approved_with_conditions|rejected",
  "action": "commit|revert|escalate",
  "confidence": 0-100,
  "summary": "1 sentence",
  "reasoning": "3-5 sentences",
  "verification_items": [{"check":"...","result":"pass|fail|partial","detail":"..."}],
  "rubric_evaluation": {
    "thoroughness": "0-5",
    "thoroughness_justification": "...",
    "evidence_check": "0-5",
    "evidence_check_justification": "...",
    "feedback_quality": "0-5",
    "feedback_quality_justification": "..."
  },
  "feedback": {
    "P_Qwen30B": {"model":"Qwen30B","role":"proposer","score":0,"strengths":[],"weaknesses":[],"improvements":[]},
    "R_Qwen14B": {"model":"Qwen14B","role":"reflector","score":0,"strengths":[],"weaknesses":[],"improvements":[]},
    "J_Selene": {"model":"Selene","role":"judge","score":0,"strengths":[],"weaknesses":[],"improvements":[]}
  },
  "handoff_comparison": {
    "better_handoff": "llm_r|python|equal",
    "reason": "why one handoff source was more useful for verification",
    "llm_r_strengths": ["..."],
    "python_strengths": ["..."]
  }
}"""

SYS_V32 = """You are an independent second-opinion verifier.

You will receive THREE handoff documents:
1. [LLM-J] — Judge LLM handoff
2. [Python] — deterministic handoff
3. [Python consolidated] — full rotation summary

Compare LLM-J vs Python. After your final verdict,
write detailed, actionable feedback per model+role:
e.g., P=Qwen30B, R=Qwen14B, J=Selene — separate feedback for each.

Return JSON:
{
  "final_verdict": "approved|approved_with_conditions|rejected",
  "action": "commit|revert|escalate",
  "confidence": 0-100,
  "summary": "1 sentence",
  "reasoning": "3-5 sentences",
  "verification_items": [{"check":"...","result":"pass|fail|partial","detail":"..."}],
  "disagreement_with_27b": [{"issue":"...","27b_verdict":"...","my_verdict":"...","detail":"..."}],
  "feedback": {
    "P_Qwen30B": {"model":"Qwen30B","role":"proposer","score":0,"strengths":[],"weaknesses":[],"improvements":[]},
    "R_Qwen14B": {"model":"Qwen14B","role":"reflector","score":0,"strengths":[],"weaknesses":[],"improvements":[]},
    "J_Selene": {"model":"Selene","role":"judge","score":0,"strengths":[],"weaknesses":[],"improvements":[]}
  },
  "handoff_comparison": {
    "better_handoff": "llm_j|python|equal",
    "reason": "why one handoff source was more useful for verification",
    "llm_j_strengths": ["..."],
    "python_strengths": ["..."]
  }
}"""

RUBRIC = """
## EVALUATION RUBRIC -- APPLY TO YOUR ROLE

### Proposer: rate each finding 0-10
- Correctness (0.35): Is it a real, verifiable issue?
- Actionability (0.30): Is there a clear fix or mitigation?
- Evidence (0.25): Is it backed by specific data or code?
- Novelty (0.10): Does it add new insight?

### Refuter: rate each verdict 0-10
- Accuracy (0.40): Is the accept/reject decision correct?
- Reasoning (0.30): Is the justification precise and specific?
- Efficiency (0.30): Is the verdict concise?

### Judge scoring rules
P = Correctness + Coverage + Precision (0-30)
R = Accuracy + Efficiency + Completeness (0-30)
gap <= 3 -> high consensus
gap > 8 -> escalate for review
consensus_score = 100 - (gap * 10)"""


# ── Phase 2: Rubric Evaluation (finding-level scores) ────────────────

SYS_RUBRIC_FINDING = """You are a rubric evaluation specialist. Assess each finding below against the standard criteria.

## Criteria (weighted)
- Correctness (0.35): Is this a real, verifiable issue?
- Actionability (0.30): Is there a clear fix or mitigation?
- Evidence (0.25): Is it backed by specific data or code?
- Novelty (0.10): Does it add new insight?

For each finding assign 0-10 per criterion with brief justification.
weighted_score = correctness*0.35 + actionability*0.30 + evidence*0.25 + novelty*0.10

Return ONLY valid JSON — no markdown, no commentary.
Schema:
{
  "rubric_evaluations": [
    {"id": "finding_id", "correctness": 0-10, "correctness_justification": "...",
     "actionability": 0-5, "actionability_justification": "...",
     "evidence": 0-5, "evidence_justification": "...",
     "novelty": 0-5, "novelty_justification": "...",
     "weighted_score": 0.00}
  ]
}"""


def rubric_evaluate_findings(findings, tag):
    """Phase 2: Evaluate each finding against rubric criteria using 7B."""
    log("\n--- Phase 2: Rubric Evaluation (finding-level) ---")
    if not findings:
        log("  No findings to evaluate — skipping rubric evaluation")
        return []

    # Build finding text for evaluation
    finding_lines = []
    for f in findings:
        fid = f.get("fid", f.get("id", "?"))
        desc = f.get("description", "").replace("\n", " ")[:200]
        sev = f.get("severity", "?")
        cat = f.get("category", "?")
        finding_lines.append(f"  [{sev}/{cat}] {fid}: {desc}")

    user_text = "Evaluate these findings against the rubric:\n\n" + "\n".join(finding_lines[:20])
    resp = call_one("Qwen7B", SYS_RUBRIC_FINDING, user_text, f"rubric_{tag}", max_tok=4096)
    rubrics = (resp or {}).get("result", {}).get("rubric_evaluations", [])

    # Build a lookup for quick access
    rubric_by_id = {r["id"]: r for r in rubrics if "id" in r}
    for f in findings:
        fid = f.get("fid", f.get("id", ""))
        if fid in rubric_by_id:
            f["rubric"] = rubric_by_id[fid]

    avg_score = 0.0
    if rubrics:
        scores = [r.get("weighted_score", 0) for r in rubrics if r.get("weighted_score") is not None]
        avg_score = sum(scores) / len(scores) if scores else 0.0

    log(f"  Evaluated {len(rubrics)} findings, avg weighted_score={avg_score:.2f}")
    return rubrics


# ── Phase 0: Python structure verification ─────────────────────────────

def python_verify(data, tag):
    log("\n--- Phase 0: Python 구조 검증 ---")
    findings_list = data.get("findings", [])
    total = len(findings_list)
    issues = []

    ids = [f.get("id", f.get("fid", f"idx_{i}")) for i, f in enumerate(findings_list)]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        issues.append({"check": "id_duplicates", "severity": "error",
                       "detail": f"Duplicate IDs: {dupes}"})
        log(f"  {FAIL} ID duplicates: {dupes}")
    else:
        log(f"  {PASS} All {total} IDs unique")

    REQUIRED = {"id", "severity", "category", "description"}
    missing = []
    for i, f in enumerate(findings_list):
        m = REQUIRED - set(f.keys())
        if m:
            missing.append((ids[i], m))
    if missing:
        issues.append({"check": "missing_fields", "severity": "error",
                       "detail": f"{len(missing)} findings missing fields: {missing}"})
        log(f"  {FAIL} {len(missing)} findings missing required fields")
    else:
        log(f"  {PASS} All {total} findings have required fields")

    VALID_SEV = {"critical", "high", "medium", "low", "pass", "fail", "partial"}
    inv_sev = [(ids[i], f.get("severity", "?"))
               for i, f in enumerate(findings_list)
               if f.get("severity", "").lower() not in VALID_SEV]
    if inv_sev:
        issues.append({"check": "invalid_severity", "severity": "warn", "detail": str(inv_sev)})
        log(f"  {WARN} Invalid severities: {inv_sev}")
    else:
        log(f"  {PASS} All severities valid")

    empty = [(ids[i], f.get("description", "")[:50])
             for i, f in enumerate(findings_list)
             if not f.get("description", "").strip()]
    if empty:
        issues.append({"check": "empty_description", "severity": "error",
                       "detail": f"{len(empty)} empty descriptions"})
        log(f"  {FAIL} {len(empty)} empty descriptions")
    else:
        log(f"  {PASS} All descriptions non-empty")

    expected = data.get("total_findings", 0)
    if expected and expected != total:
        issues.append({"check": "count_mismatch", "severity": "error",
                       "detail": f"meta={expected} actual={total}"})
        log(f"  {FAIL} Count mismatch: meta={expected} actual={total}")
    else:
        log(f"  {PASS} Finding count matches metadata ({total})")

    no_src = [ids[i] for i, f in enumerate(findings_list) if not f.get("source_file")]
    if no_src:
        log(f"  {WARN} {len(no_src)} findings missing source_file")

    sev_dist = {}
    for f in findings_list:
        s = f.get("severity", "unknown").lower()
        sev_dist[s] = sev_dist.get(s, 0) + 1
    log(f"  Severity distribution: {sev_dist}")

    result = {"total_findings": total, "issues_found": len(issues),
              "issues": issues, "severity_distribution": sev_dist}
    save(f"pyverify_{tag}", tag, result)
    log(f"  -> {len(issues)} issues, {total} findings checked")
    return result


# ── Model constants ─────────────────────────────────────────────────

MOCK_RESULT = {
    "result": {
        "findings": [
            {"id": "F001", "severity": "critical", "category": "bug", "description": "Mock finding for dry-run test", "file": "mock.py"},
            {"id": "F002", "severity": "high", "category": "security", "description": "Another mock finding", "file": "mock.py"},
        ],
        "rubric_evaluations": [
            {"id": "F001", "correctness": 8, "correctness_justification": "Real issue",
             "actionability": 7, "actionability_justification": "Clear fix",
             "evidence": 9, "evidence_justification": "Code evidence present",
             "novelty": 6, "novelty_justification": "Known pattern",
             "weighted_score": 7.65},
            {"id": "F002", "correctness": 5, "correctness_justification": "Unclear",
             "actionability": 4, "actionability_justification": "No mitigation",
             "evidence": 6, "evidence_justification": "Partial evidence",
             "novelty": 3, "novelty_justification": "Well known",
             "weighted_score": 4.75},
        ],
        "verdicts": [
            {"id": "F001", "verdict": "accept", "reason": "Valid dry-run finding"},
            {"id": "F002", "verdict": "reject", "reason": "Not reproducible in dry-run"},
        ],
        "P_score": 25, "R_score": 22,
        "P_rubric": {"correctness": 8, "coverage": 9, "precision": 8},
        "R_rubric": {"accuracy": 7, "efficiency": 8, "completeness": 7},
        "decision": "APPROVED", "consensus_score": 85,
        "approved": ["F001"], "rejected": ["F002"],
        "rubric_evaluation": {
            "fairness": 8, "fairness_justification": "Balanced scoring",
            "clarity": 7, "clarity_justification": "Clear report",
            "consistency": 8, "consistency_justification": "Consistent decisions",
        },
        "report": {
            "summary": "Mock dry-run report summary",
            "top_issues": ["F001: critical bug in mock.py"],
            "quality_notes": {"strengths": ["Good coverage"], "weaknesses": ["Limited data"]},
            "recommendation": "Commit after review",
        },
        "handoff": {
            "source": "dry_run_mock",
            "executive_summary": "Dry-run test handoff",
            "approved": [{"id": "F001", "severity":"critical","category":"bug","finding":"dry-run","approval_rationale":"test"}],
            "rejected": [{"id": "F002", "severity":"high","category":"security","finding":"dry-run","rejection_rationale":"test"}],
            "critical_remaining": [], "unresolved_count": 0,
            "verifier_priority": ["Check F001"], "quality_red_flags": [],
            "p_score": 25, "r_score": 22, "j_consensus": 85,
        },
        "handoff_comparison": {
            "better_handoff": "equal", "reason": "Both sources agree in dry-run",
            "llm_r_strengths": ["Rich descriptions"], "python_strengths": ["Deterministic counts"],
        },
        "final_verdict": "approved_with_conditions", "action": "commit", "confidence": 80,
        "summary": "Dry-run verification passed with conditions",
        "reasoning": "Mock reasoning for dry-run test",
        "verification_items": [{"check":"All findings verified","result":"pass","detail":"Mock verification"}],
        "feedback": {
            "P_Qwen30B": {"model":"Qwen30B","role":"proposer","score":80,"strengths":["Good coverage"],"weaknesses":["Needs more detail"],"improvements":["Add more context"]},
            "R_Qwen14B": {"model":"Qwen14B","role":"reflector","score":75,"strengths":["Accurate"],"weaknesses":["Brief reasoning"],"improvements":["Elaborate on rejections"]},
            "J_Selene": {"model":"Selene","role":"judge","score":85,"strengths":["Fair"],"weaknesses":["Could be more detailed"],"improvements":["Add more rationale"]},
        },
    },
    "usage": {"prompt_tokens": 500, "completion_tokens": 200},
    "timings": {"prompt_per_second": 10, "predicted_per_second": 5},
    "elapsed_ms": 1500,
}

P_MODEL = "Qwen30B"   # review-p mode (Pod B :8080)
R_MODEL = "Qwen14B"   # review-r mode (Pod B :8080)
J_MODEL = "Codestral" # review-j mode (Pod B :8080)

MODEL_METADATA = {
    "Qwen3B":  {"file": "Qwen2.5-Coder-3B-Instruct.Q8_0.gguf",  "size": "3.1GB", "port": 8082, "mode": "day"},
    "Qwen7B":  {"file": "Qwen2.5-Coder-7B-Instruct.Q8_0.gguf",  "size": "7.6GB", "port": 8080, "mode": "test-7b"},
    "Qwen14B": {"file": "Qwen2.5-Coder-14B-Instruct.Q8_0.gguf", "size": "15.7GB","port": 8080, "mode": "review-r"},
    "Qwen30B": {"file": "Qwen3-Coder-30B-A3B-Instruct-Q4_K_S.gguf","size":"17GB","port":8080, "mode":"review-p"},
    "Codestral":{"file":"Codestral-22B-v0.1-Q4_K_M.gguf",       "size":"12.4GB","port":8080, "mode":"review-j"},
    "Qwen27B": {"file": "Qwen3.6-27B-Q4_K_M.gguf",             "size": "16GB", "port": 8081, "mode": "verify"},
}

def model_info(key):
    m = MODEL_METADATA.get(key, {})
    return f"{key}({m.get('file','?')} {m.get('size','?')} :{m.get('port','?')})"


# ── Model call with single-model-at-a-time guarantee ──────────────────

def call_one(model_name, sys_prompt, user_text, tag_label, max_tok=2048):
    """kill_all -> start only this model -> LLM call."""
    if DRY_RUN:
        log(f"  [DRY] call_one({model_name}) → mock response")
        return MOCK_RESULT
    ok = ensure_model(model_name)
    if not ok:
        abort("컨테이너 시작 실패", model_name,
              f"{model_name} 컨테이너가 300s 내에 준비되지 않음")
    return llm_call(
        [{"role": "system", "content": sys_prompt},
         {"role": "user", "content": user_text}],
        model_name, max_tokens=max_tok, label=tag_label)


# ── Python handoff compiler (deterministic, no hallucination) ────────────

def compile_handoff_single(r, round_num, with_rubric):
    """Python-compiled handoff from P-R-J result data."""
    n_approved = len(r.get("approved", []))
    n_rejected = len(r.get("rejected", []))
    return {
        "source": "python_compiled",
        "p_model": r["p_model"],
        "r_model": r["r_model"],
        "j_model": r["j_model"],
        "round": round_num,
        "with_rubric": with_rubric,
        "P_score": r["P_score"],
        "R_score": r["R_score"],
        "consensus": r["consensus"],
        "decision": r["decision"],
        "approved_count": n_approved,
        "rejected_count": n_rejected,
        "approved_ids": sorted(r.get("approved", [])),
        "rejected_ids": sorted(r.get("rejected", [])),
        "report_summary": r.get("report_summary", ""),
    }


def compile_handoff(prj_results, round_num, with_rubric):
    """Consolidated handoff from P-R-J results."""
    r = prj_results[0] if prj_results else {}
    return {
        "source": "python_consolidated",
        "round": round_num,
        "with_rubric": with_rubric,
        "P_score": r.get("P_score", 0),
        "R_score": r.get("R_score", 0),
        "consensus": r.get("consensus", 0),
        "decision": r.get("decision", ""),
        "total_approved": len(r.get("approved", [])),
        "total_rejected": len(r.get("rejected", [])),
        "all_approved_ids": sorted(r.get("approved", [])),
        "all_rejected_ids": sorted(r.get("rejected", [])),
        "report_summary": r.get("report_summary", ""),
        "top_issues": r.get("report_top_issues", [])[:5],
    }


# ── Round runner ──────────────────────────────────────────────────────

def _run_prj(state, tag, rubric_append):
    """P-R-J 1회 패스. kill_all → P(30B) → R(Qwen14B) → J(Selene) → state 저장."""
    log("\n--- Phase 3: P-R-J (P=30B,R=Qwen14B,J=Selene) ---")
    handoff_fragment = {}

    # P — gets findings by severity + 30B context
    p_max = 4096
    p_r = call_one(P_MODEL, SYS_P + rubric_append,
                   state.build_context("prj_p"),
                   f"P_{tag}", max_tok=p_max)
    save(f"p_{tag}", tag, p_r)
    p_findings = (p_r or {}).get("result", {}).get("findings", [])

    # R
    r_max = 2048
    r_r = call_one(R_MODEL, SYS_R + rubric_append,
                   f"Proposer findings:\n{json.dumps(p_findings, ensure_ascii=False, indent=2)[:4000]}",
                   f"R_{tag}", max_tok=r_max)
    save(f"r_{tag}", tag, r_r)
    r_verdicts = (r_r or {}).get("result", {}).get("verdicts", [])

    # J
    j_max = 2048
    j_r = call_one(J_MODEL, SYS_J + rubric_append,
                   state.build_context("prj_j", {"rotation_index": 0})
                   + f"\n\n### P findings:\n"
                   + json.dumps(p_findings, ensure_ascii=False, indent=2)[:2000]
                   + f"\n\n### R verdicts:\n"
                   + json.dumps(r_verdicts, ensure_ascii=False, indent=2)[:2000],
                   f"J_{tag}", max_tok=j_max)
    save(f"j_{tag}", tag, j_r)

    j_res = (j_r or {}).get("result", {})
    j_report = j_res.get("report", {})
    j_handoff = j_res.get("handoff", {})
    handoff_fragment = j_handoff
    prj_result = {
        "p_model": P_MODEL, "r_model": R_MODEL, "j_model": J_MODEL,
        "P_score": j_res.get("P_score", 0),
        "R_score": j_res.get("R_score", 0),
        "consensus": j_res.get("consensus_score", 0),
        "decision": j_res.get("decision", ""),
        "approved": j_res.get("approved", []),
        "rejected": j_res.get("rejected", []),
        "p_count": len(p_findings),
        "r_count": len(r_verdicts),
        "p_elapsed_ms": (p_r or {}).get("elapsed_ms", 0),
        "r_elapsed_ms": (r_r or {}).get("elapsed_ms", 0),
        "j_elapsed_ms": (j_r or {}).get("elapsed_ms", 0),
        "report_summary": j_report.get("summary", ""),
        "report_top_issues": j_report.get("top_issues", []),
        "report_recommendation": j_report.get("recommendation", ""),
    }
    state.add_prj_rotation(prj_result)
    ps = prj_result['P_score']
    rs = prj_result['R_score']
    cs = prj_result['consensus']
    slack_msg = (f"[P-R-J] *Round {state.round_num}*\n"
                 f"P={P_MODEL}→{ps} | R={R_MODEL}→{rs} | J={J_MODEL}→consensus={cs}\n")
    if j_report.get("summary"):
        slack_msg += f"> {j_report['summary'][:120]}"
    slack_send(slack_msg)
    return prj_result, handoff_fragment, p_findings, r_verdicts


# ── R handoff writer ───────────────────────────────────────────────

SYS_R_HANDOFF = """You are a senior reviewer (R) writing the final handoff document after a complete P-R-J review cycle.

The full cycle is complete:
- **P (30B)**: Proposed findings with severity/category
- **You (R, 14B)**: Reviewed each finding — accepted or rejected
- **J (Selene)**: Final scoring and consolidated decision

Your job: synthesize ALL of the above into a comprehensive handoff for the **final verifier (27B)**.

Grounding rules:
- ALL finding IDs must come from the actual data below. Do NOT fabricate.
- Approved items = accepted by R AND approved by J
- Rejected items = rejected by R OR rejected by J
- "critical_remaining" = IDs of rejected findings with severity critical/high
- Include specific severity, category, and rationale for each finding

Return ONLY valid JSON — no markdown, no commentary.

Schema:
{
  "handoff": {
    "source": "R(Qwen14B)_handoff",
    "executive_summary": "1-2 sentence overview of the full P-R-J cycle including key decisions",
    "approved": [
      {"id": "F001", "severity": "critical|high|medium|low", "category": "bug|security|...",
       "finding": "brief description (under 150 chars)",
       "approval_rationale": "why this was accepted by both R and J"}
    ],
    "rejected": [
      {"id": "F002", "severity": "...", "category": "...",
       "finding": "brief description",
       "rejection_rationale": "why this was rejected"}
    ],
    "critical_remaining": [],
    "unresolved_count": 0,
    "verifier_priority": [
      "specific item for verifier to double-check (with concrete reason and finding ID)"
    ],
    "quality_red_flags": ["systemic concern across multiple findings"],
    "p_score": 0-30,
    "r_score": 0-30,
    "j_consensus": 0-100,
    "rubric_evaluation": {
      "completeness": "0-10",
      "completeness_justification": "...",
      "accuracy": "0-10",
      "accuracy_justification": "...",
      "clarity": "0-10",
      "clarity_justification": "..."
    }
  }
}"""


def _trim_handoff(text, label=""):
    """Trim handoff document to essential fields only (prepill defense)."""
    try:
        data = json.loads(text) if isinstance(text, str) else text
    except (json.JSONDecodeError, TypeError):
        return str(text)[:2000]

    trimmed = {"_trimmed": True, "_source": label}

    # LLM-R handoff: {"source":"llm_r","handoff":{...}}
    hoff = data if "executive_summary" in data else data.get("handoff", data)
    if isinstance(hoff, dict) and hoff.get("executive_summary"):
        trimmed.update({
            "source": "llm_r",
            "executive_summary": hoff.get("executive_summary", "")[:200],
            "unresolved_count": hoff.get("unresolved_count", 0),
            "critical_remaining": hoff.get("critical_remaining", [])[:3],
            "p_score": hoff.get("p_score", 0),
            "r_score": hoff.get("r_score", 0),
            "j_consensus": hoff.get("j_consensus", 0),
            "approved_ids": [a.get("id","") for a in (hoff.get("approved") or [])[:5]],
            "rejected_ids": [r.get("id","") for r in (hoff.get("rejected") or [])[:5]],
            "verifier_priority": (hoff.get("verifier_priority") or [])[:3],
        })
        return json.dumps(trimmed, ensure_ascii=False, indent=2)

    # Python handoff: {"P_score":N, "decision":"...", ...}
    if "P_score" in data and "decision" in data:
        trimmed.update({
            "source": "python",
            "P_score": data.get("P_score"),
            "R_score": data.get("R_score"),
            "consensus": data.get("consensus"),
            "decision": data.get("decision"),
            "approved_count": data.get("approved_count", data.get("total_approved", 0)),
            "rejected_count": data.get("rejected_count", data.get("total_rejected", 0)),
            "report_summary": (data.get("report_summary") or "")[:200],
        })
        return json.dumps(trimmed, ensure_ascii=False, indent=2)

    # Fallback: truncate
    s = json.dumps(data, ensure_ascii=False, indent=2)
    return s[:2000] + ("\n... (truncated)" if len(s) > 2000 else "")


def run_round(round_num, with_rubric, resume_state_path=None):
    tag = f"r{round_num}_{'rubric' if with_rubric else 'norubric'}"
    log(f"\n{'='*60}")
    log(f"ROUND {round_num}: {'WITH RUBRIC' if with_rubric else 'NO RUBRIC'}")
    log(f"{'='*60}")

    rubric_append = f"

{RUBRIC}" if with_rubric else ""

    if resume_state_path:
        # Resume: load existing pipeline_state, skip Phase 0/1
        with open(resume_state_path) as f:
            state_data = json.load(f)
        data = load_input()
        state = PipelineState(round_num, with_rubric, data, existing_data=state_data)
        v7_res = state_data.get("30b_verify", {})
        py_res = state_data.get("python_verify", {})
        log("RESUME: 기존 state 로드, Python/30B 검증 건너뜀")
    else:
        data = load_input()
        state = PipelineState(round_num, with_rubric, data)

        # Phase 0: Python verify (no LLM)
        py_res = python_verify(data, tag)
        state.add_phase("python_verify", py_res)
        slack_send(f"[P-R-J] *Round {round_num}* Python verify: {py_res['issues_found']} issues ({py_res['total_findings']} findings)")

        # Phase 1: 7B verify (lightweight pre-filter)
        log("\n--- Phase 1: 7B Verify ---")
        v7 = call_one("Qwen7B", SYS_V27 + rubric_append,
                       state.build_context("30b_verify"),
                       f"7B_{tag}")
        v7_res = (v7 or {}).get("result", {})
        save(f"7b_{tag}", tag, v7)
        v7v = v7_res.get('final_verdict', '?')
        c7v = v7_res.get('confidence', '?')
        log(f"  7B verdict={v7v} confidence={c7v}")
        state.add_phase("30b_verify", v7_res)
        slack_send(f"[P-R-J] *Round {round_num}* 7B verify: *{v7v}* (confidence={c7v})")

        # Phase 2: Rubric evaluation (finding-level scores via 7B)
        findings = state.data.get("input", {}).get("findings", [])
        rubric_results = rubric_evaluate_findings(findings, tag)
        state.add_phase("rubric_evaluation", {"evaluations": rubric_results})

    # Phase 3: P-R-J 1 pass (single fixed rotation)
    prj_result, handoff_fragment, p_findings, r_verdicts = _run_prj(state, tag, rubric_append)

    # ── Phase 3.5: R(14B) writes the final handoff ──
    # R has full context: P findings, its own verdicts, and J's final decision.
    # R produces a comprehensive structured handoff for the 27B verifier.
    log("\n--- Phase 3.5: R(14B) writes final handoff ---")

    r_ctx_parts = [
        f"=== P-R-J CYCLE COMPLETE ===",
        f"P_model={P_MODEL} R_model={R_MODEL} J_model={J_MODEL}\n",
        f"=== P PROPOSED FINDINGS ({len(p_findings)}) ===",
    ]
    for pf in p_findings:
        r_ctx_parts.append(
            f"  {pf['id']} [{pf.get('severity','?')}/{pf.get('category','?')}]: {pf.get('description','')[:200]}")
    r_ctx_parts.append(f"\n=== R VERDICTS ({len(r_verdicts)}) ===")
    for rv in r_verdicts:
        r_ctx_parts.append(f"  {rv['id']}: {rv.get('verdict','?')} — {rv.get('reason','')[:150]}")
    r_ctx_parts.append(f"\n=== J FINAL DECISION ===")
    r_ctx_parts.append(f"  P_score={prj_result.get('P_score','?')} R_score={prj_result.get('R_score','?')}")
    r_ctx_parts.append(f"  consensus={prj_result.get('consensus','?')} decision={prj_result.get('decision','?')}")
    r_ctx_parts.append(f"  approved={prj_result.get('approved',[])}")
    r_ctx_parts.append(f"  rejected={prj_result.get('rejected',[])}")
    r_ctx_parts.append(f"  summary: {prj_result.get('report_summary','')}")
    for ti in (prj_result.get('report_top_issues') or []):
        r_ctx_parts.append(f"  top issue: {ti}")
    r_handoff_ctx = "\n".join(r_ctx_parts)

    r_hoff_resp = call_one(R_MODEL, SYS_R_HANDOFF, r_handoff_ctx, f"handoff_R_{tag}")
    r_hoff_data = (r_hoff_resp or {}).get("result", {}).get("handoff", {})
    save(f"handoff_r_{tag}", tag, {
        "source": "llm_r", "handoff": r_hoff_data,
        "p_findings_count": len(p_findings), "r_verdicts_count": len(r_verdicts),
        "model_metadata": {k: MODEL_METADATA.get(k) for k in (P_MODEL, R_MODEL, J_MODEL)}})

    # ── Save handoffs: 1 LLM-R + 1 Python ────────────
    handoff_models = {
        "P": {"key": P_MODEL, **MODEL_METADATA.get(P_MODEL, {})},
        "R": {"key": R_MODEL, **MODEL_METADATA.get(R_MODEL, {})},
        "J": {"key": J_MODEL, **MODEL_METADATA.get(J_MODEL, {})},
    }
    hoff_meta = json.dumps(handoff_models, ensure_ascii=False, indent=2)
    hoff_header = f"## Model Metadata (for future reference)\n{hoff_meta}\n\n"

    llm_save = {"source": "llm_r", "round": round_num,
                "r_model": R_MODEL, "handoff": r_hoff_data}
    save(f"handoff_llm_{tag}", tag, llm_save)
    llm_text = (hoff_header
                + f"## Handoff (R={model_info(R_MODEL)}) [LLM-R]\n"
                + json.dumps(llm_save, ensure_ascii=False, indent=2))
    log(f"  R handoff: {len(r_hoff_data.get('approved',[]))} approved, "
        f"{len(r_hoff_data.get('rejected',[]))} rejected")

    py_single = compile_handoff_single(prj_result, round_num, with_rubric)
    py_single["model_metadata"] = handoff_models
    save(f"handoff_py_{tag}", tag, py_single)
    py_text = f"## Handoff [Python]\n" + json.dumps(py_single, ensure_ascii=False, indent=2)
    pyc_text = json.dumps(py_single, ensure_ascii=False, indent=2)
    log(f"  -> 1 LLM-R + 1 Python handoffs saved")

    state.add_phase("handoffs", {"llm_texts": [llm_text], "py_texts": [py_text],
                                 "consolidated": py_single})

    # ── P-R-J phase complete ──
    log(f"\n{'='*60}")
    log(f"P-R-J 완료")
    log(f"{'='*60}")
    slack_send(f"[P-R-J] *Round {round_num}* P-R-J 완료. "
               f"결과: {prj_result.get('decision','?')} (consensus={prj_result.get('consensus','?')})")

    # ── STOP: PRJ complete. Now run 27B verify ──
    summary = {
        "round": round_num, "with_rubric": with_rubric,
        "python_verify": {"issues_found": py_res["issues_found"], "total": py_res["total_findings"]},
        "30b": {"verdict": v7_res.get("final_verdict","?"), "confidence": v7_res.get("confidence",0)},
        "prj": [prj_result],
    }

    # ─────────────────────────────────────────────────────────────────
    # Phase 4: 27B verify (production final gate)
    # ─────────────────────────────────────────────────────────────────
    # ── Trim handoffs for prepill defense ──────────────────────
    # Extract JSON portion from markdown-wrapped handoff texts
    def _extract_json_part(txt):
        """Return (header, json_str) from '## Header\\n{...}' handoff text."""
        brace = txt.find("{")
        if brace == -1:
            return txt, ""
        return txt[:brace], txt[brace:]

    llm_header, llm_json = _extract_json_part(llm_text)
    py_header, py_json = _extract_json_part(py_text)
    trimmed_llm = _trim_handoff(llm_json, "llm_r")
    trimmed_py = _trim_handoff(py_json, "python")
    trimmed_pyc = _trim_handoff(pyc_text, "python_consolidated")

    verifier_input = (
        "Below are 3 handoff documents:\n"
        "- 1 LLM-R\n"
        "- 1 Python\n"
        "- 1 Python consolidated\n\n"
        + f"\n\n---\n\n{llm_header}{trimmed_llm}\n\n---\n\n"
        + f"{py_header}{trimmed_py}\n\n---\n\n"
        + f"## Consolidated Handoff [Python]\n{trimmed_pyc}\n\n"
        "Compare LLM-R vs Python. "
        "In your 'handoff_comparison' field, state which source "
        "(llm_r or python) was more useful for verification overall and why."
    )

    log("\n--- Phase 4: 27B Verify ---")
    v27_ctx = state.build_context("final_verify") + "\n\n" + verifier_input
    v27 = call_one("Qwen27B", SYS_V27 + rubric_append,
                   v27_ctx, f"27B_{tag}")
    v27_res = (v27 or {}).get("result", {})
    save(f"v27b_{tag}", tag, v27)
    state.add_phase("27b_verify", v27_res)
    v27v = v27_res.get('final_verdict', '?')
    c27 = v27_res.get('confidence', '?')
    hc27 = v27_res.get('handoff_comparison', {})
    fb27 = v27_res.get('feedback', {})
    log(f"  27B verdict={v27v} confidence={c27}")
    log(f"  27B handoff preference: {hc27.get('better_handoff', '?')}")
    for role_key, role_fb in fb27.items():
        imp = role_fb.get("improvements", [])
        if imp:
            log(f"  feedback {role_key}: {imp[0][:80]}")
    slack_send(f"[P-R-J] *Round {round_num}* 27B verifier: *{v27v}* "
               f"(conf={c27}) handoff={hc27.get('better_handoff','?')}")

    # ── Phase 5: Feedback loop — save patterns to DB, re-run P-R-J ──
    fb_count = save_feedback_to_db(fb27, tag)
    if fb_count > 0:
        log(f"  Saved {fb_count} feedback entries to activity_log")
        # Clear feedback cache so next LLM calls pick up new patterns
        import lib.llm_client
        lib.llm_client._feedback_cache = {}
        lib.llm_client._feedback_ts = 0.0
        log("  _feedback_cache cleared")

        fb_tag = f"{tag}_fb"
        log("\n--- Phase 5a: P-R-J with injected feedback ---")
        fb_prj_result, fb_hoff, fb_pf, fb_rv = _run_prj(state, fb_tag, rubric_append)

        # Phase 5b: R handoff with feedback
        log("\n--- Phase 5b: R handoff (post-feedback) ---")
        r_ctx_parts = [
            f"=== P-R-J CYCLE (POST-FEEDBACK) ===",
            f"P_model={P_MODEL} R_model={R_MODEL} J_model={J_MODEL}\n",
            f"=== P PROPOSED FINDINGS ({len(fb_pf)}) ===",
        ]
        for pf in fb_pf:
            r_ctx_parts.append(
                f"  {pf['id']} [{pf.get('severity','?')}/{pf.get('category','?')}]: {pf.get('description','')[:200]}")
        r_ctx_parts.append(f"\n=== R VERDICTS ({len(fb_rv)}) ===")
        for rv in fb_rv:
            r_ctx_parts.append(f"  {rv['id']}: {rv.get('verdict','?')} — {rv.get('reason','')[:150]}")
        r_ctx_parts.append(f"\n=== J FINAL DECISION ===")
        r_ctx_parts.append(f"  P_score={fb_prj_result.get('P_score','?')} R_score={fb_prj_result.get('R_score','?')}")
        r_ctx_parts.append(f"  consensus={fb_prj_result.get('consensus','?')} decision={fb_prj_result.get('decision','?')}")
        r_ctx_parts.append(f"  approved={fb_prj_result.get('approved',[])}")
        r_ctx_parts.append(f"  rejected={fb_prj_result.get('rejected',[])}")
        r_ctx_parts.append(f"  summary: {fb_prj_result.get('report_summary','')}")
        for ti in (fb_prj_result.get('report_top_issues') or []):
            r_ctx_parts.append(f"  top issue: {ti}")
        fb_hoff_resp = call_one(R_MODEL, SYS_R_HANDOFF, "\n".join(r_ctx_parts), f"handoff_R_{fb_tag}")
        fb_hoff_data = (fb_hoff_resp or {}).get("result", {}).get("handoff", {})
        save(f"handoff_r_{fb_tag}", fb_tag, {
            "source": "llm_r_feedback", "handoff": fb_hoff_data,
            "p_findings_count": len(fb_pf), "r_verdicts_count": len(fb_rv),
            "model_metadata": {k: MODEL_METADATA.get(k) for k in (P_MODEL, R_MODEL, J_MODEL)}})
        fb_hoff_header = hoff_header  # reuse from phase 3.5
        fb_llm_text = fb_hoff_header + f"## Handoff (R={model_info(R_MODEL)}) [LLM-R]\n" + json.dumps(
            {"source": "llm_r_feedback", "handoff": fb_hoff_data}, ensure_ascii=False, indent=2)
        fb_py_single = compile_handoff_single(fb_prj_result, round_num, with_rubric)
        fb_py_single["model_metadata"] = handoff_models
        fb_py_text = f"## Handoff [Python]\n" + json.dumps(fb_py_single, ensure_ascii=False, indent=2)
        fb_pyc_text = json.dumps(fb_py_single, ensure_ascii=False, indent=2)
        log(f"  1 LLM-R + 1 Python handoffs (post-feedback) saved")

        # Phase 5c: 27B verify (post-feedback)
        log("\n--- Phase 5c: 27B Verify (post-feedback) ---")
        fb_llm_h, fb_llm_j = _extract_json_part(fb_llm_text)
        fb_py_h, fb_py_j = _extract_json_part(fb_py_text)
        fb_v27_ctx = state.build_context("final_verify") + "\n\n" + (
            "Below are 3 handoff documents:\n"
            "- 1 LLM-R\n- 1 Python\n- 1 Python consolidated\n\n"
            + f"\n\n---\n\n{fb_llm_h}{_trim_handoff(fb_llm_j, 'llm_r_fb')}"
            + f"\n\n---\n\n{fb_py_h}{_trim_handoff(fb_py_j, 'python_fb')}"
            + f"\n\n---\n\n## Consolidated Handoff [Python]\n"
            + f"{_trim_handoff(fb_pyc_text, 'python_consolidated_fb')}\n\n"
            "Compare LLM-R vs Python. "
            "In your 'handoff_comparison' field, state which source "
            "(llm_r or python) was more useful for verification overall and why."
        )
        fb_v27 = call_one("Qwen27B", SYS_V27 + rubric_append, fb_v27_ctx, f"27B_{fb_tag}")
        fb_v27_res = (fb_v27 or {}).get("result", {})
        save(f"v27b_{fb_tag}", fb_tag, fb_v27)
        fb_v27v = fb_v27_res.get('final_verdict', '?')
        fb_c27 = fb_v27_res.get('confidence', '?')
        log(f"  27B (feedback round) verdict={fb_v27v} confidence={fb_c27}")
        slack_send(f"[P-R-J] *Round {round_num}* 27B (feedback): *{fb_v27v}* (conf={fb_c27})")

        summary["feedback_loop"] = {
            "saved_count": fb_count,
            "fb_prj": [fb_prj_result],
            "fb_27b": {"verdict": fb_v27_res.get("final_verdict","?"), "confidence": fb_v27_res.get("confidence",0)},
        }
        log("\n--- Phase 5 Complete: Feedback loop executed ---")
    else:
        log("  No feedback entries to save — skipping Phase 5 feedback loop")

    # Pull Phase 2 rubric results from state if available
    rubric_evals = state.data.get("rubric_evaluation", {}).get("evaluations", []) if not resume_state_path else []

    summary["27b"] = {"verdict": v27_res.get("final_verdict","?"), "confidence": v27_res.get("confidence",0),
                      "feedback": fb27, "handoff_comparison": hc27,
                      "rubric_evaluation": v27_res.get("rubric_evaluation", {}),
                      "phase2_rubric": {"evaluations": rubric_evals,
                                        "count": len(rubric_evals)}}
    save(f"summary_{tag}", tag, summary)
    return summary


def save_feedback_to_db(fb27, tag):
    """Save 27B's per-model feedback to activity_log for feedback loop injection.

    Each role's feedback is saved as an activity_log entry with:
    - weaknesses + improvements → findings (edge_case patterns for _extract_findings)
    - strengths → verification_items (gold_standard patterns)

    Returns count of entries saved.
    """
    if DRY_RUN:
        log("  [DRY] save_feedback_to_db() → simulated 1 entry")
        return 1  # trigger feedback loop for full dry-run coverage
    if not fb27:
        return 0

    count = 0
    for role_key, role_fb in fb27.items():
        if not isinstance(role_fb, dict):
            continue
        model = role_fb.get("model", "")
        role = role_fb.get("role", "")
        score = role_fb.get("score", 0)
        strengths = role_fb.get("strengths", [])
        weaknesses = role_fb.get("weaknesses", [])
        improvements = role_fb.get("improvements", [])

        if not weaknesses and not strengths:
            continue

        findings = []
        for i, w in enumerate(weaknesses):
            fix = improvements[i] if i < len(improvements) else "Review and address this weakness."
            findings.append({
                "description": str(w)[:300], "severity": "medium", "category": "quality",
                "fix": str(fix)[:300],
            })
        verification_items = [
            {"check": str(s)[:300], "result": "pass", "detail": "Strength confirmed in 27B review"}
            for s in strengths
        ]

        title = f"27B feedback: {model} ({role})"
        summary = f"27B review feedback for {model} ({role}): score={score}/100, {len(strengths)} strengths, {len(weaknesses)} weaknesses"

        body = {
            "findings": findings,
            "verification_items": verification_items,
            "source": "27b_feedback",
            "feedback_role": role,
            "feedback_model": model,
            "score": score,
        }

        body_json = json.dumps(body, ensure_ascii=False).replace("'", "''")
        title_esc = title.replace("'", "''")
        summary_esc = summary.replace("'", "''")

        sql = (
            "INSERT INTO activity_log "
            "(type, source, title, summary, body, model, summary_status, queue_status, exec_status) "
            "VALUES ("
            f"'verify_result', '27b_feedback', '{title_esc}', "
            f"'{summary_esc}', '{body_json}', 'deepseek-v4-flash', "
            "'raw', 'reviewed', 'DONE'"
            ")"
        )
        r = subprocess.run(
            ["podman", "exec", "-i", "postgres", "psql", "-U", "postgres",
             "-d", "devforge_app", "-c", sql],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0:
            count += 1
            log(f"  Saved feedback for {model} ({role}) to activity_log")
        else:
            log(f"  Failed to save feedback for {model} ({role}): {r.stderr[:100]}")

    return count


# ── Queue mode: P-R-J batch consumer ────────────────────────────────

def _get_turn(turn_id):
    """Query turns table by UUID."""
    sql = (
        "SELECT id, user_turn, thinking, text, source_message_id, "
        "  created_at, conversation_id, seq "
        f"FROM turns WHERE id = '{esc_sql(turn_id)}'::uuid"
    )
    row = psql(sql)
    if not row:
        return None
    parts = [p.strip() for p in row.split("|")]
    if len(parts) < 8:
        return None
    return {
        "id": parts[0], "user_turn": parts[1],
        "thinking": parts[2] or None, "text": parts[3],
        "source_message_id": parts[4], "created_at": parts[5],
        "conversation_id": parts[6], "seq": int(parts[7]) if parts[7].strip() else 0,
    }


def _get_facts(turn_id):
    """Query extracted facts for a turn from review_facts (non-marker)."""
    sql = (
        "SELECT fact_index, fact_type, evidence, extract_model, verdict "
        f"FROM review_facts "
        f"WHERE turn_id = '{esc_sql(turn_id)}'::uuid "
        f"  AND verdict != 'system' "
        "ORDER BY fact_index ASC"
    )
    rows = psql(sql)
    if not rows:
        return []
    facts = []
    for line in rows.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 3:
            continue
        facts.append({
            "fact_index": int(parts[0]) if parts[0].strip() else 0,
            "fact_type": parts[1].strip(),
            "evidence": parts[2].strip(),
            "extract_model": parts[3].strip() if len(parts) > 3 else "",
            "verdict": parts[4].strip() if len(parts) > 4 else "",
        })
    return facts


def _read_pending_items(limit=5):
    """Read pending extract_results from activity_log, with optional day_review reference."""
    sql = (
        "SELECT al.id, al.body, al.title, al.summary, al.created_at, "
        "  dr.body as day_review_body "
        "FROM activity_log al "
        "LEFT JOIN LATERAL ("
        "  SELECT body FROM activity_log "
        "  WHERE type='day_review' "
        "    AND body->>'turn_id' = al.body->>'turn_id' "
        "  LIMIT 1"
        ") dr ON true "
        "WHERE al.queue_status='pending' AND al.type='extract_result' "
        "ORDER BY al.created_at ASC "
        f"LIMIT {limit}"
    )
    rows = psql(sql)
    if not rows:
        return []
    items = []
    for line in rows.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 3:
            continue
        try:
            body = json.loads(parts[1].strip())
        except json.JSONDecodeError:
            continue
        day_review = None
        if len(parts) > 5 and parts[5].strip():
            try:
                day_review = json.loads(parts[5].strip())
            except json.JSONDecodeError:
                pass
        items.append({
            "log_id": int(parts[0].strip()),
            "body": body,
            "title": parts[2].strip(),
            "summary": parts[3].strip() if len(parts) > 3 else "",
            "created_at": parts[4].strip() if len(parts) > 4 else "",
            "day_review": day_review,
        })
    return items


def _build_p_context(turn, facts, body, day_review=None):
    """Build P(30B) prompt context from turn + facts + MCP metadata + optional day_review."""
    parts = [
        "=== TURN ===",
        f"User: {turn.get('user_turn', '')[:2000]}",
        f"Thinking: {(turn.get('thinking') or '')[:2000]}",
        f"Response: {(turn.get('text') or '')[:2000]}",
        "",
        f"=== EXTRACTED FACTS ({len(facts)}) ===",
    ]
    for f in facts:
        parts.append(f"  [{f.get('fact_type','?')}] {f.get('evidence','')[:300]}")
    mcp = body.get("mcp", {})
    if mcp:
        parts.extend(["", "=== MCP METADATA ===",
                      f"  tldr: {mcp.get('tldr', '')}",
                      f"  intent: {mcp.get('intent', '')}"])
        ents = mcp.get("entities", {})
        if ents:
            files = ents.get("files", [])[:5]
            funcs = ents.get("functions", [])[:5]
            parts.append(f"  entities: files={len(ents.get('files',[]))}, "
                         f"funcs={len(ents.get('functions',[]))}")
            if files:
                for f in files:
                    parts.append(f"    file: {f}")
            if funcs:
                for f in funcs:
                    parts.append(f"    func: {f}")
        tags = mcp.get("tags", [])
        if tags:
            parts.append(f"  tags: {tags[:10]}")

    if day_review:
        jr = day_review.get("J_results", {})
        dr_findings = day_review.get("P_results", [])
        dr_verdicts = day_review.get("R_results", [])
        parts.extend([
            "",
            "=== DAY PRE-REVIEW REFERENCE ===",
            f"  [note: day review by 7B+3B, may contain hallucinations]",
            f"  P_score={jr.get('P_score','?')} R_score={jr.get('R_score','?')}",
            f"  decision={jr.get('decision','?')}",
            f"  approved={jr.get('approved',[])}",
            f"  rejected={jr.get('rejected',[])}",
        ])
        if dr_findings:
            parts.append(f"  Day findings ({len(dr_findings)}):")
            for f in dr_findings[:5]:
                parts.append(f"    [{f.get('severity','?')}] {f.get('description','')[:120]}")
        if dr_verdicts:
            parts.append(f"  Day verdicts ({len(dr_verdicts)}):")
            for v in dr_verdicts[:5]:
                parts.append(f"    {v.get('id','?')}: {v.get('verdict','?')}")
        parts.extend([
            "",
            "Perform your OWN independent review. Day results are reference only.",
            "Do NOT rely on day findings — verify everything yourself.",
            "",
        ])
    return "\n".join(parts)


def _batch_p(items, rubric_append):
    """P(30B) batch: load once, review all items."""
    log(f"\n--- P(30B) Batch Review ({len(items)} items) ---")
    if DRY_RUN:
        log("  [DRY] mock P batch")
        MOCK = [{"id": "M001", "severity": "medium", "category": "quality",
                  "description": "Dry-run P finding for extract review", "file": "extract"}]
        return [MOCK for _ in items]

    ok = ensure_model(P_MODEL)
    if not ok:
        log(f"  FAILED to load {P_MODEL}")
        return [[] for _ in items]

    results = []
    for idx, item in enumerate(items):
        turn_id = item["body"].get("turn_id", "")
        turn = _get_turn(turn_id)
        if not turn:
            log(f"  [{idx+1}/{len(items)}] Turn not found: {turn_id[:8]}")
            results.append([])
            continue
        facts = _get_facts(turn_id)
        log(f"  [{idx+1}/{len(items)}] {turn_id[:8]}: {len(facts)} facts")
        ctx = _build_p_context(turn, facts, item["body"], day_review=item.get("day_review"))
        resp = llm_call(
            [{"role": "system", "content": SYS_P + rubric_append},
             {"role": "user", "content": ctx}],
            model=P_MODEL, max_tokens=4096, label=f"P_queue_{idx}")
        findings = resp.get("result", {}).get("findings", [])
        log(f"    P: {len(findings)} findings")
        results.append(findings)
    return results


def _batch_r(items, p_results, rubric_append):
    """R(14B) batch: load once, reflect on all P findings."""
    log(f"\n--- R(14B) Batch Reflection ({len(items)} items) ---")
    if DRY_RUN:
        log("  [DRY] mock R batch")
        MOCK = [{"id": "M001", "verdict": "accept", "reason": "Dry-run R verdict"}]
        return [MOCK for _ in items]

    ok = ensure_model(R_MODEL)
    if not ok:
        log(f"  FAILED to load {R_MODEL}")
        return [[] for _ in items]

    results = []
    for idx, (item, p_findings) in enumerate(zip(items, p_results)):
        if not p_findings:
            results.append([])
            continue
        ctx = f"Proposer findings:\n{json.dumps(p_findings, ensure_ascii=False, indent=2)[:4000]}"
        resp = llm_call(
            [{"role": "system", "content": SYS_R + rubric_append},
             {"role": "user", "content": ctx}],
            model=R_MODEL, max_tokens=2048, label=f"R_queue_{idx}")
        verdicts = resp.get("result", {}).get("verdicts", [])
        log(f"    R: {len(verdicts)} verdicts")
        results.append(verdicts)
    return results


def _batch_j(items, p_results, r_results, rubric_append):
    """J(Codestral) batch: load once, score all P-R pairs."""
    log(f"\n--- J(Codestral) Batch Scoring ({len(items)} items) ---")
    if DRY_RUN:
        log("  [DRY] mock J batch")
        MOCK = {"P_score": 25, "R_score": 22, "decision": "APPROVED",
                "consensus_score": 85, "approved": ["M001"], "rejected": []}
        return [MOCK for _ in items]

    ok = ensure_model(J_MODEL)
    if not ok:
        log(f"  FAILED to load {J_MODEL}")
        return [None for _ in items]

    results = []
    for idx, (item, p_findings, r_verdicts) in enumerate(zip(items, p_results, r_results)):
        ctx_parts = [
            f"P findings ({len(p_findings)}):\n",
            json.dumps(p_findings, ensure_ascii=False, indent=2)[:2000],
            f"\nR verdicts ({len(r_verdicts)}):\n",
            json.dumps(r_verdicts, ensure_ascii=False, indent=2)[:2000],
        ]
        resp = llm_call(
            [{"role": "system", "content": SYS_J + rubric_append},
             {"role": "user", "content": "\n".join(ctx_parts)}],
            model=J_MODEL, max_tokens=2048, label=f"J_queue_{idx}")
        jr = resp.get("result", {})
        log(f"    J: P_score={jr.get('P_score','?')} R_score={jr.get('R_score','?')} "
            f"decision={jr.get('decision','?')}")
        results.append(jr if jr.get("decision") in ("APPROVED", "REJECT") else None)
    return results


def run_queue_mode(limit=5):
    """Read pending extract_results, run batched P-R-J, set queue_status='reviewed'."""
    log("=" * 60)
    log("P-R-J Queue Consumer Mode")
    log("=" * 60)

    rubric_append = f"\n\n{RUBRIC}"
    items = _read_pending_items(limit)
    if not items:
        log("  No pending extract_results found")
        return {"processed": 0, "total": 0}

    log(f"  Found {len(items)} pending item(s)")
    p_results = _batch_p(items, rubric_append)
    r_results = _batch_r(items, p_results, rubric_append)
    j_results = _batch_j(items, p_results, r_results, rubric_append)

    processed = 0
    for idx, (item, j_res) in enumerate(zip(items, j_results)):
        log_id = item["log_id"]
        if j_res:
            divergence = (j_res.get("P_score", 0) > 25
                          and len(j_res.get("rejected", [])) > 3)
            body_update = {"prj_result": j_res}
            if divergence:
                body_update["high_divergence"] = True
            psql_ok(
                f"UPDATE activity_log "
                f"SET queue_status='reviewed', "
                f"  body = body || '{json.dumps(body_update, ensure_ascii=False)}'::jsonb "
                f"WHERE id={log_id} AND queue_status='pending'"
            )
            log(f"  [{idx+1}/{len(items)}] Log#{log_id}: queue_status→reviewed "
                f"({j_res.get('decision','?')})")
            processed += 1
        else:
            log(f"  [{idx+1}/{len(items)}] Log#{log_id}: SKIP (P-R-J incomplete), "
                f"will retry next night")

    log(f"\nQueue mode complete: {processed}/{len(items)} processed")
    return {"processed": processed, "total": len(items)}


# ── Main ──────────────────────────────────────────────────────────────

def run_extract(with_rubric, mcp_model="Qwen7B"):
    """Phase -1: 3B extract → Python verify → 7B MCP.

    Pod A day (3B Q8_0 :8082) for extract + Pod B day (7B Q8_0 :8080) for MCP."""
    log("\n--- Phase -1: 3B Extract + MCP ---")
    if DRY_RUN:
        log("  [DRY] Extract phase skipped")
        return

    log("  Pod A day(3B:8082) + Pod B day(7B:8080) for MCP...")
    with open(MODE_FILE_B, "w") as f:
        f.write("MODE=day")
    with open(MODE_FILE_A, "w") as f:
        f.write("MODE=day")
    kill_all()
    subprocess.run(["systemctl", "--user", "start", "container-devforge-qwen.service"],
                   capture_output=True, timeout=60)
    subprocess.run(["systemctl", "--user", "start", "container-devforge-swap.service"],
                   capture_output=True, timeout=60)
    ok_a = wait_health(8082)
    ok_b = wait_health(8080)
    if not (ok_a and ok_b):
        log(f"  Day mode containers not ready: A={ok_a} B={ok_b}")
        slack_send(f":warning: Extract phase — day mode containers not ready")
        return
    log(f"  :8082 ready (3B) + :8080 ready (3B/7B)")

    from extract_pipeline import extract_pipeline
    result = extract_pipeline(
        turn_id=None,
        limit=50,
        dry_run=False,
        mcp_model=mcp_model,
    )
    log(f"  Extract result: {result['processed']} processed, "
        f"{result['failed']} failed, {result['facts']} facts")

def main():
    if "--queue" in sys.argv:
        limit = 5
        for i, a in enumerate(sys.argv):
            if a == "--limit" and i + 1 < len(sys.argv):
                limit = int(sys.argv[i + 1])
        result = run_queue_mode(limit=limit)
        log(f"Queue mode complete: {result['processed']}/{result['total']} processed")
        return

    if RESUME_PRJ:
        log("RESUME PRJ MODE")
        log("기존 state 로드, Python 검증/30B 건너뛰고 P-R-J 1회 패스부터 재개\n")
        resume_path = os.path.join(EXPER_DIR, "pipeline_state_r1_norubric.json")
        if not os.path.exists(resume_path):
            log(f"ERROR: resume state not found: {resume_path}")
            sys.exit(1)
        slack_send(":repeat: *P-R-J 재개* — 30B verify 결과 유지, P-R-J 1회 패스부터 재시작")
        t_all = time.monotonic()
        r1 = run_round(1, with_rubric=False, resume_state_path=resume_path)
    else:
        log("P-R-J 고정 역할 실험 시작")
        log("파이프라인: 3B 추출 → Python 검증 → 7B 검증 → Rubric 평가 → P-R-J(P=30B,R=14B,J=Selene) → 27B 검증 → 피드백 루프(재검증)")
        log("메모리 관리: phase 전환마다 podman stop -> 필요한 컨테이너만 시작 (Pod B 순차 swap)")
        log("P-R-J: 30B(P) + Qwen2.5-14B(R) + SeleneMini Q8(J) + 7B Q8 verify + 27B Q4 verify\n")
        slack_send(
            ":hammer: *P-R-J 고정 역할 실험 시작*\n"
            "3B추출 → Python검증 → 7B → P-R-J → 27B → 피드백루프 → 재검증"
        )
        t_all = time.monotonic()

        # Phase -1: 3B extract
        if "--skip-extract" not in sys.argv:
            run_extract(with_rubric=False)
        else:
            log("  --skip-extract: extract phase skipped")

        r1 = run_round(1, with_rubric=False)

    r1_prj = r1.get("prj", [])

    elapsed_total = (time.monotonic() - t_all) / 60

    log(f"\n{'='*60}")
    log(f"P-R-J 고정 역할 실험 Round 1 완료 (runtime: {elapsed_total:.0f}분)")
    log(f"{'='*60}")
    for r in r1_prj:
        log(f"  P={r.get('p_model','?')}({r.get('P_score','?')})"
            f" R={r.get('r_model','?')}({r.get('R_score','?')})"
            f" J={r.get('j_model','?')} → consensus={r.get('consensus','?')} {r.get('decision','?')}")

    # Rubric evaluation summary
    rubric_meta = r1.get('27b', {}).get('phase2_rubric', {})
    rubric_evals_list = rubric_meta.get('evaluations', [])
    if rubric_evals_list:
        scores = [r.get('weighted_score', 0) for r in rubric_evals_list if r.get('weighted_score') is not None]
        if scores:
            low_n = sum(1 for s in scores if s < 5.0)
            log(f"  Rubric: avg={sum(scores)/len(scores):.2f} low(<5.0)={low_n}/{len(scores)}")
    v27b = r1.get('27b', {})
    if v27b:
        log(f"  27B verify: {v27b.get('verdict','?')} (confidence={v27b.get('confidence','?')})")

    slack_send(
        f"[P-R-J] *Round 1 완료* (control)\n"
        f"Python verify: {r1.get('python_verify',{}).get('issues_found',0)} issues\n"
        f"30B: {r1.get('30b',{}).get('verdict','?')} ({r1.get('30b',{}).get('confidence','?')})\n"
        f"P-R-J: {r1_prj[0].get('decision','?') if r1_prj else 'N/A'}\n"
        + (f"27B: {v27b.get('verdict','?')} (conf={v27b.get('confidence','?')})\n" if v27b else "")
        + f"실행시간: {elapsed_total:.0f}분"
    )

    fpath = save("summary", "final", r1)

    log(f"\nResults: {EXPER_DIR}/")
    log(f"Summary: {fpath}")
    log("Done.")
    log("Done.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise  # allow abort() (sys.exit) to work normally
    except Exception as e:
        log(f"UNHANDLED ERROR: {e}")
        import traceback
        log(traceback.format_exc())
        slack_send(f":no_entry: *P-R-J 실험 중단* — 예상치 못한 오류\n> `{e}`")
        sys.exit(1)

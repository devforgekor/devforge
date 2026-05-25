#!/usr/bin/env python3
"""DevForge Multi-Agent LLM Debate Orchestrator v3.1

MoE debate — all models served sequentially via Pod B supervisor (:8081).
Pod A is idle during debate (available for future lightweight models).

Role assignment (all MoE except 32B):
  P — Qwen3-30B-A3B (18GB)  — code-specialized proposal generation
  R — Nemotron-Cascade-2  (17GB) — systematic critique, reasoning
  J — GLM-4.7-Flash       (17GB) — neutral judge
  S — Qwen2.5-Coder-32B   (17GB) — final synthesis (dense, IQ4_XS)

All models load sequentially on Pod B; supervisor evicts old model cache on each switch.

Usage:
  python3 scripts/debate.py --question "File: ...\nTask: ..." [--skip-drag] [--dry-run]
"""
import json
import os
import random
import re
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── Paths ──────────────────────────────────────────────────────────────────
SWITCH_FILE = "/opt/ai_data/debate/switch/model-switch.json"
SESSIONS_DIR = Path("/opt/ai_data/debate_sessions")

# ── Model catalogue (MoE lineup) ──────────────────────────────────────────
MODELS: Dict[str, Dict[str, Any]] = {
    # GLM-4.7-Flash — DRAG + Judge + Summary (supervisor-managed on :8081)
    "glm-47-flash": {
        "filename": "GLM-4.7-Flash-Q4_K_M.gguf",
        "port": 8081, "ctx": 4096, "threads": 4, "mlock": 0,
        "max_tokens": 1024, "temperature": 0.1,
        "system_prompt_support": True,
        "bench_load_s": 370, "bench_toks": 10.0,
        "cache_ram": 1024,
    },
    # Pod B — supervisor-managed (switching, port 8081)
    "qwen3-30b-a3b": {
        "filename": "Qwen3-30B-A3B-Q4_K_M.gguf",
        "port": 8081, "ctx": 4096, "threads": 4, "mlock": 0,
        "max_tokens": 1024, "temperature": 0.1,
        "system_prompt_support": True,
        "bench_load_s": 360, "bench_toks": 4.0,
        "cache_ram": 2048,
    },
    "nemotron-cascade-2": {
        "filename": "Nemotron-Cascade-2-30B-A3B.IQ4_XS.gguf",
        "port": 8081, "ctx": 4096, "threads": 4, "mlock": 0,
        "max_tokens": 1024, "temperature": 0.6, "top_p": 0.95,
        "system_prompt_support": True,
        "bench_load_s": 360, "bench_toks": 4.0,
        "cache_ram": 2048,
    },
    "qwen-32b": {
        "filename": "Qwen2.5-Coder-32B-Instruct-IQ4_XS.gguf",
        "port": 8081, "ctx": 4096, "threads": 4, "mlock": 1,
        "max_tokens": 1024, "temperature": 0.1,
        "system_prompt_support": True,
        "cache_ram": 2048,
        "bench_load_s": 360, "bench_toks": 0.5,
    },
}

# ── Prompt templates ───────────────────────────────────────────────────────

PROMPTS = {
    # ── Round 0: DRAG (GLM: file analysis + debate framing) ──
    "drag_analysis": {
        "system": (
            "You are a code analysis strategist preparing context for a multi-agent "
            "code debate. Your job: read the target file, understand its structure, "
            "and set up a clear debate framework that the Proposer and Refuter will use."
        ),
        "user": (
            "Task: {question}\n\n"
            "Target file content:\n```python\n{file_content}\n```\n\n"
            "Analyze and produce:\n"
            "1. Code structure overview — key functions, classes, patterns affected\n"
            "2. Change scope — what exactly needs to be modified and where\n"
            "3. Key decision points — 3-5 specific questions the debate must resolve\n"
            "   (e.g., naming conventions, abstraction level, error handling strategy)\n"
            "4. Constraints — existing patterns that must be preserved\n\n"
            "Output STRICT JSON:\n"
            '{{"structure_overview": "...", '
            '"change_scope": "...", '
            '"decision_points": ["point1", "point2", ...], '
            '"constraints": ["constraint1", "constraint2", ...]}}'
        ),
    },

    # ── Rounds 1-4: DART ──
    "dart_proposer": {
        "system": (
            "You are a solution PROPOSER in a code debate. Your role:\n"
            "1. Build on your previous arguments using the debate context.\n"
            "2. Clearly state what you AGREE and DISAGREE with in the refuter's last response.\n"
            "3. Strengthen weak points. Abandon positions that evidence contradicts.\n"
            "4. Ground your proposal in the DRAG analysis framework."
        ),
        "user": (
            "Task: {question}\n\n"
            "DRAG Context (from pre-debate analysis):\n{drag_context}\n\n"
            "Round History:\n{history_summary}\n\n"
            "Judge's last assessment: consensus={consensus_score}%\n"
            "Disagreements: {disagreement_points}\n\n"
            "Refuter's last argument:\n{refuter_last_output}\n\n"
            "Your task: Respond with a strengthened proposal.\n"
            "Output STRICT JSON (no extra text):\n"
            '{{"logic_summary": "max 3 sentences", '
            '"code_snippet": "```python\\n...\\n```", '
            '"confidence_score": 0-100, '
            '"disagreement_points": ["point1", "point2"]}}'
        ),
    },
    "dart_refuter": {
        "system": (
            "You are a CRITICAL REFUTER in a code debate. Find weaknesses, "
            "propose alternatives, and challenge assumptions. Be constructive — "
            "every critique must come with an alternative suggestion."
        ),
        "user": (
            "Task: {question}\n\n"
            "DRAG Context (from pre-debate analysis):\n{drag_context}\n\n"
            "Round History:\n{history_summary}\n\n"
            "Judge's last assessment: consensus={consensus_score}%\n"
            "Disagreements: {disagreement_points}\n\n"
            "Proposer's latest argument:\n{proposer_last_output}\n\n"
            "Your task:\n"
            "1. Identify logical flaws, missing edge cases, or performance issues.\n"
            "2. Propose a concrete alternative for each weakness found.\n\n"
            "Output STRICT JSON (no extra text, no markdown wrapper):\n"
            '{{"logic_summary": "max 3 sentences", '
            '"code_snippet": "```python\\n...\\n```", '
            '"confidence_score": 0-100, '
            '"disagreement_points": ["point1", "point2"]}}'
        ),
    },
    "dart_judge": {
        "system": (
            "You are a 3-person jury panel for a code debate:\n"
            "- Juror 1: Security expert\n"
            "- Juror 2: Performance optimization expert\n"
            "- Juror 3: Code readability/maintainability expert\n\n"
            "Rules:\n"
            "- You see two ANONYMIZED proposals (Draft Alpha, Draft Beta). Order is random.\n"
            "- IGNORE: response length, comment style, politeness, formatting verbosity.\n"
            "- JUDGE ONLY: logical correctness, code integrity, factual accuracy.\n"
            "- If consensus < 70%, note what information would resolve the dispute."
        ),
        "user": (
            "Task: {question}\n\n"
            "DRAG Context:\n{drag_context}\n\n"
            "Draft Alpha:\n{proposal_a_anonymized}\n\n"
            "Draft Beta:\n{proposal_b_anonymized}\n\n"
            "Each juror: which draft is stronger and why?\n"
            "Overall consensus score (0-100).\n\n"
            "Output STRICT JSON:\n"
            '{{"jury_opinions": {{"security": "...", "performance": "...", "readability": "..."}}, '
            '"consensus_score": 0-100, '
            '"winner": "alpha|beta|tie", '
            '"disagreement_analysis": "1-sentence summary of key unresolved issues"}}'
        ),
    },

    # ── Round 5: Synthesis ──
    "history_summary": {
        "system": (
            "You are a debate historian. Condense a multi-round code debate into "
            "a structured summary suitable for the final synthesizer. "
            "Include: key arguments from both sides, resolved vs disputed points, "
            "and your recommended direction."
        ),
        "user": (
            "Task: {question}\n\n"
            "Full Debate History:\n{full_history}\n\n"
            "Consensus Trend: {consensus_trend}\n\n"
            "Summarize the debate. Include:\n"
            "1. Key arguments from Proposer and Refuter\n"
            "2. Points that were resolved vs. still disputed\n"
            "3. Recommended approach for the final synthesizer\n\n"
            "Output as plain text (no JSON, no markdown). Max 500 words."
        ),
    },
    "final_synthesis": {
        "system": (
            "You are the final synthesizer for a multi-agent code debate. "
            "You have access to the complete debate record and the debate summary. "
            "Your job: produce the definitive, executable final code modification."
        ),
        "user": (
            "Task: {question}\n\n"
            "Debate Summary:\n{history_summary}\n\n"
            "Consensus Trend: {consensus_trend}\n\n"
            "DRAG Context:\n{drag_context}\n\n"
            "Produce the final answer:\n"
            "1. Executable final code diff (complete, unified diff format).\n"
            "2. Decision summary — why this solution was chosen.\n"
            "3. Security concerns note.\n"
            "4. Performance considerations note.\n\n"
            "Output STRICT JSON:\n"
            '{{"diff": "--- a/file\\n+++ b/file\\n@@ ...", '
            '"decision_summary": "3-5 sentence reasoning", '
            '"security_notes": ["note1", "note2"], '
            '"performance_notes": ["note1", "note2"], '
            '"confidence": 0.0-1.0}}'
        ),
    },
}


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _parse_json(raw: str) -> Optional[dict]:
    """Thin wrapper — delegates to shared Recovery Ladder in lib.llm.json_parser."""
    from lib.llm.json_parser import parse_llm_json
    return parse_llm_json(raw)


def _build_messages(prompt_key: str, model_id: str, **kwargs) -> List[Dict[str, str]]:
    """Build chat messages respecting system_prompt_support flag."""
    template = PROMPTS[prompt_key]
    model_cfg = MODELS[model_id]
    messages = []

    if model_cfg["system_prompt_support"] and template["system"]:
        messages.append({"role": "system", "content": template["system"]})

    user_content = template["user"].format(**kwargs)
    messages.append({"role": "user", "content": user_content})
    return messages


def _write_switch_file(model_id: str) -> None:
    """Write model-switch.json for supervisor on :8081 to detect."""
    cfg = MODELS[model_id]
    data = {
        "model_file": cfg["filename"],
        "port": cfg["port"],
        "ctx": cfg["ctx"],
        "threads": cfg["threads"],
        "mlock": cfg["mlock"],
        "cache_ram": cfg.get("cache_ram", 0),
    }
    os.makedirs(os.path.dirname(SWITCH_FILE), exist_ok=True)
    with open(SWITCH_FILE, "w") as f:
        json.dump(data, f, indent=2)
    print(f"  [switch] wrote {cfg['filename']} (mlock={cfg['mlock']})")


def _evict_file_cache(filepath: str) -> bool:
    """Evict a file's pages from kernel page cache via posix_fadvise(DONTNEED).

    Targeted eviction — only affects this file, not the entire cache.
    Returns True on success, False if file not found or eviction failed.
    """
    try:
        fd = os.open(filepath, os.O_RDONLY)
        try:
            st_size = os.fstat(fd).st_size
            os.posix_fadvise(fd, 0, st_size, os.POSIX_FADV_DONTNEED)
            print(f"  [evict] {os.path.basename(filepath)}: {st_size // (1024*1024)}MB evicted")
            return True
        finally:
            os.close(fd)
    except FileNotFoundError:
        print(f"  [evict] file not found: {filepath}")
        return False
    except Exception as e:
        print(f"  [evict] failed: {e}")
        return False




def _poll_health(port: int, timeout: int = 240, backoff_base: float = 2.0) -> bool:
    """Poll :<port>/health with exponential backoff."""
    health_url = f"http://127.0.0.1:{port}/health"
    print(f"  [health] waiting for :{port} (timeout={timeout}s)...")
    start = time.monotonic()
    attempt = 0
    while time.monotonic() - start < timeout:
        try:
            req = urllib.request.Request(health_url)
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read())
                    if data.get("status") == "ok":
                        elapsed = time.monotonic() - start
                        print(f"  [health] :{port} ready after {elapsed:.0f}s")
                        return True
        except Exception:
            pass
        attempt += 1
        delay = min(backoff_base * (2 ** attempt), 8.0)
        time.sleep(delay)
    print(f"  [health] :{port} TIMEOUT after {timeout}s")
    return False


def _is_retryable(err_msg: str) -> bool:
    """Check if error is a transient connection issue worth retrying."""
    retryable = ("timed out", "Remote end closed", "Connection reset", "Connection aborted")
    return any(p in err_msg for p in retryable)


def _read_file_content(file_path: str) -> Optional[str]:
    """Read target file for DRAG analysis. Returns None if file not found."""
    try:
        return Path(file_path).read_text()
    except FileNotFoundError:
        # Try relative to /opt/projects/server
        alt = Path("/opt/projects/server") / file_path.lstrip("/")
        try:
            return alt.read_text()
        except Exception:
            return None
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════
# DebateSession
# ═══════════════════════════════════════════════════════════════════════════

class DebateSession:
    """MoE debate orchestrator — all models on Pod B (:8081) supervisor.

    Pod B sequential switching:
      Round 0: GLM-4.7-Flash (DRAG)
      Rounds 1-4: Qwen3-30B-A3B → Nemotron-Cascade-2 → GLM-4.7-Flash (Judge)
      Round 5: GLM-4.7-Flash (Summary) → Qwen2.5-Coder-32B (Synthesis)

    Pod A is idle during debate (available for future lightweight models).
    """

    def __init__(
        self,
        question: str,
        method: str = "drag",
        skip_drag: bool = False,
        dry_run: bool = False,
    ):
        self.question = question
        self.method = method
        self.skip_drag = skip_drag
        self.dry_run = dry_run

        self.session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.state_dir = SESSIONS_DIR / self.session_id
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.current_round: int = 0
        self.consensus_scores: List[int] = []
        self.winner_map: List[Dict] = []
        self.drag_context: str = ""

        # Fixed role assignment — all models served via Pod B (:8081)
        self.drag_model = "glm-47-flash"         # Pod B — DRAG analysis
        self.proposer_model = "qwen3-30b-a3b"     # Pod B
        self.refuter_model = "nemotron-cascade-2"  # Pod B
        self.judge_model = "glm-47-flash"          # Pod B — Judge (switches each round)
        self.summary_model = "glm-47-flash"        # Pod B — History summary
        self.synthesizer_model = "qwen-32b"        # Pod B

    # ── Persistence ────────────────────────────────────────────────────

    def _save_state(self, entry: Dict) -> None:
        entry.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        entry.setdefault("round", self.current_round)
        path = self.state_dir / "state.jsonl"
        with open(path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ── Model switching ────────────────────────────────────────────────

    def switch_model(self, model_id: str) -> bool:
        """Ensure model is ready on Pod B (:8081) via supervisor switch."""
        cfg = MODELS[model_id]

        if self.dry_run:
            print(f"  [dry-run] switch to {model_id} ({cfg['filename']}) on :8081")
            return True

        _write_switch_file(model_id)
        time.sleep(3)
        return _poll_health(port=8081, timeout=cfg.get("bench_load_s", 120) + 60)

    # ── LLM calling ────────────────────────────────────────────────────

    def call_llm(self, messages: List[Dict], model_id: str, max_tokens: Optional[int] = None) -> Optional[str]:
        """Call llama-server and return raw text response.

        Timeout = max_tokens / bench_toks + 300s buffer.
        MoE models at ~4-10 tok/s, 32B at ~0.5 tok/s.
        On connection error, retries once with halved max_tokens.
        """
        cfg = MODELS[model_id]
        port = cfg["port"]
        llm_url = f"http://127.0.0.1:{port}/v1/chat/completions"

        mt = max_tokens if max_tokens is not None else cfg["max_tokens"]
        body = {
            "messages": messages,
            "temperature": cfg["temperature"],
            "max_tokens": mt,
        }
        if "top_p" in cfg:
            body["top_p"] = cfg["top_p"]

        if self.dry_run:
            print(f"  [dry-run] LLM call :{port}: {len(body['messages'])} msgs, "
                  f"max_tokens={body['max_tokens']}")
            return '{"dry_run": true}'

        for attempt in range(2):
            gen_rate = cfg.get("bench_toks", 2.0)
            timeout = int(body["max_tokens"] / gen_rate) + 300
            print(f"  [llm] calling {model_id} on :{port} (max_tokens={body['max_tokens']}, timeout={timeout}s"
                  f"{', retry' if attempt > 0 else ''})...")
            t_start = time.monotonic()
            try:
                data = json.dumps(body).encode()
                req = urllib.request.Request(
                    llm_url, data=data,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    result = json.loads(resp.read())
                    elapsed = time.monotonic() - t_start
                    content = result["choices"][0]["message"]["content"]
                    print(f"  [llm] response in {elapsed:.1f}s ({len(content)} chars)")
                    return content
            except Exception as e:
                err_msg = str(e)
                print(f"  [llm] ERROR: {e}")
                # Retry with halved max_tokens on connection/timeout errors
                if attempt == 0 and _is_retryable(err_msg) and body["max_tokens"] > 256:
                    body["max_tokens"] = max(body["max_tokens"] // 2, 256)
                    body["temperature"] = min(body["temperature"], 0.1)
                    print(f"  [llm] retrying with max_tokens={body['max_tokens']}...")
                    time.sleep(3)
                    continue
                return None

        return None

    def call_llm_json(self, prompt_key: str, model_id: str, retry: int = 1, **kwargs) -> Optional[dict]:
        """Call LLM and parse JSON response. Retry once with strict prompt on failure."""
        messages = _build_messages(prompt_key, model_id, **kwargs)
        raw = self.call_llm(messages, model_id)
        if raw is None:
            return None

        parsed = _parse_json(raw)
        if parsed is not None:
            return parsed

        if retry > 0:
            print(f"  [json] parse failed, retrying with strict prompt...")
            strict_msg = ("Your previous response was not valid JSON. "
                          "Output STRICT JSON ONLY. No extra text, no markdown.")
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": strict_msg})
            raw2 = self.call_llm(messages, model_id)
            if raw2:
                parsed2 = _parse_json(raw2)
                if parsed2 is not None:
                    return parsed2
            combined = (raw or "") + "\n" + (raw2 or "")
            parsed = _parse_json(combined)

        return parsed

    # ── Anonymize ──────────────────────────────────────────────────────

    def _shuffle_proposals(
        self, prop_a: dict, prop_b: dict
    ) -> Tuple[dict, dict, str, str]:
        """Randomly assign Alpha/Beta labels for blind judging."""
        if random.random() < 0.5:
            labeled = [("alpha", prop_a), ("beta", prop_b)]
            mapping = {"alpha": self.proposer_model, "beta": self.refuter_model}
        else:
            labeled = [("alpha", prop_b), ("beta", prop_a)]
            mapping = {"alpha": self.refuter_model, "beta": self.proposer_model}

        self._save_state({
            "type": "winner_map",
            "alpha": mapping["alpha"],
            "beta": mapping["beta"],
        })
        self.winner_map.append({
            "round": self.current_round,
            "alpha": mapping["alpha"],
            "beta": mapping["beta"],
        })

        return (
            {"label": labeled[0][0], **labeled[0][1]},
            {"label": labeled[1][0], **labeled[1][1]},
            mapping["alpha"],
            mapping["beta"],
        )

    def _resolve_winner(self, verdict: dict) -> str:
        if not self.winner_map:
            return verdict.get("winner", "unknown")
        wm = self.winner_map[-1]
        winner_label = verdict.get("winner", "tie")
        if winner_label in ("alpha", "beta"):
            return wm[winner_label]
        return winner_label

    # ── Early exit ─────────────────────────────────────────────────────

    def _check_early_exit(self) -> Optional[str]:
        if not self.consensus_scores:
            return None
        latest = self.consensus_scores[-1]
        if latest >= 90:
            return f"consensus >= 90% ({latest}%)"
        if len(self.consensus_scores) >= 2:
            improvement = latest - self.consensus_scores[-2]
            if improvement < 5:
                return f"stagnation: improvement < 5% ({improvement}%)"
        return None

    # ═══════════════════════════════════════════════════════════════════
    # Round handlers
    # ═══════════════════════════════════════════════════════════════════

    def round_0_drag(self) -> bool:
        """DRAG: GLM analyzes target file and sets debate context.

        Pod B (:8081) loads GLM-4.7-Flash for the first time.
        Returns False if skipped.
        """
        if self.skip_drag:
            print("\n─── Round 0 (DRAG) SKIPPED (--skip-drag) ───\n")
            self._save_state({"type": "round_skip", "reason": "--skip-drag flag"})
            # Load GLM anyway for Judge role later
            if not self.switch_model(self.drag_model):
                print("  [ERROR] GLM failed to load on Pod B (skip_drag path)")
                return False
            return False

        print(f"\n{'='*60}")
        print(f"Round 0: DRAG — Context Analysis (GLM-4.7-Flash)")
        print(f"{'='*60}\n")
        self._save_state({"type": "round_start", "phase": "drag"})

        # Load GLM on Pod B via supervisor
        if not self.switch_model(self.drag_model):
            print("  [ERROR] GLM failed to load on Pod B")
            return False

        # Extract and read target file
        file_path = _extract_file_path(self.question)
        file_content = _read_file_content(file_path) if file_path else None
        if not file_content:
            print(f"  [WARN] Could not read file: {file_path}")
            file_content = f"# File not found: {file_path}\n# Proceeding with question only."

        print(f"  [drag] Analyzing {file_path} ({len(file_content)} chars)...")

        analysis = self.call_llm_json(
            "drag_analysis", self.drag_model,
            question=self.question,
            file_content=file_content[-12000:],  # Truncate to avoid context overflow
        )
        if not analysis:
            print("  [ERROR] DRAG analysis failed")
            return False

        self.drag_context = json.dumps(analysis, indent=2)
        self._save_state({"type": "llm_response", "phase": "drag_analysis",
                          "model": self.drag_model, "output": analysis,
                          "file_path": file_path})

        decision_points = len(analysis.get("decision_points", []))
        print(f"\n  [drag] Analysis complete: {decision_points} decision points identified")
        print(f"  [drag] DRAG analysis complete — GLM will be reloaded for Judge + Summary")
        return True

    def round_1_to_4_dart(self) -> bool:
        """DART: Rounds 1-4 — Proposer → Refuter → Judge.

        Pod B switching: P (Qwen3-30B) → R (Nemotron-Cascade-2) → J (GLM-4.7-Flash)
        All three load sequentially via supervisor; old model cache evicted per switch.
        """
        proposer_output = None
        refuter_output = None
        last_disagreement = "N/A (first round)"
        consecutive_failures = 0

        for rnd in range(1, 5):
            self.current_round = rnd

            reason = self._check_early_exit()
            if reason:
                print(f"\n─── Early exit at Round {rnd}: {reason} ───")
                self._save_state({"type": "early_exit", "reason": reason})
                return True

            print(f"\n{'='*60}")
            print(f"Round {rnd}: DART Debate")
            print(f"{'='*60}\n")
            self._save_state({"type": "round_start", "phase": "dart"})

            history_summary = json.dumps({
                "round": rnd,
                "prior_consensus": self.consensus_scores,
            })
            drag_ctx = self.drag_context or json.dumps({"note": "DRAG skipped, no pre-debate context"})

            # A — Proposer (Pod B: Qwen3-30B-A3B)
            if not self.switch_model(self.proposer_model):
                print("  [ERROR] switch to proposer model failed")
                consecutive_failures += 1
                if consecutive_failures >= 2:
                    print("  [ABORT] 2 consecutive switch failures — debate cannot continue")
                    return False
                continue
            proposer_output = self.call_llm_json(
                "dart_proposer", self.proposer_model,
                question=self.question,
                drag_context=drag_ctx,
                history_summary=history_summary,
                consensus_score=str(self.consensus_scores[-1] if self.consensus_scores else "N/A"),
                disagreement_points=last_disagreement,
                refuter_last_output=json.dumps(refuter_output, indent=2) if refuter_output else "N/A (first round)",
            )
            if not proposer_output:
                print("  [ERROR] proposer failed")
                consecutive_failures += 1
                if consecutive_failures >= 2:
                    print("  [ABORT] 2 consecutive round failures — debate cannot continue")
                    return False
                continue
            self._save_state({"type": "llm_response", "phase": "dart_proposer",
                              "model": self.proposer_model, "output": proposer_output})

            # B — Refuter (Pod B: Nemotron-Cascade-2)
            if not self.switch_model(self.refuter_model):
                print("  [ERROR] switch to refuter model failed")
                consecutive_failures += 1
                if consecutive_failures >= 2:
                    print("  [ABORT] 2 consecutive switch failures — debate cannot continue")
                    return False
                continue
            refuter_output = self.call_llm_json(
                "dart_refuter", self.refuter_model,
                question=self.question,
                drag_context=drag_ctx,
                history_summary=history_summary,
                consensus_score=str(self.consensus_scores[-1] if self.consensus_scores else "N/A"),
                disagreement_points=last_disagreement,
                proposer_last_output=json.dumps(proposer_output, indent=2),
            )
            if not refuter_output:
                print("  [ERROR] refuter failed")
                consecutive_failures += 1
                if consecutive_failures >= 2:
                    print("  [ABORT] 2 consecutive round failures — debate cannot continue")
                    return False
                continue
            self._save_state({"type": "llm_response", "phase": "dart_refuter",
                              "model": self.refuter_model, "output": refuter_output})

            # C — Judge (Pod B: GLM-4.7-Flash, switched in from Refuter)
            if not self.switch_model(self.judge_model):
                print("  [ERROR] judge model switch failed")
                continue
            alpha, beta, _, _ = self._shuffle_proposals(proposer_output, refuter_output)
            judge_output = self.call_llm_json(
                "dart_judge", self.judge_model,
                question=self.question,
                drag_context=drag_ctx,
                proposal_a_anonymized=json.dumps(alpha, indent=2),
                proposal_b_anonymized=json.dumps(beta, indent=2),
            )
            if not judge_output:
                print("  [ERROR] judge failed")
                continue

            winner = self._resolve_winner(judge_output)
            score = judge_output.get("consensus_score", 0)
            self.consensus_scores.append(score)
            last_disagreement = judge_output.get("disagreement_analysis", "no specific disagreements")

            consecutive_failures = 0  # reset on successful round
            self._save_state({
                "type": "judge_verdict",
                "consensus_score": score,
                "winner_label": judge_output.get("winner"),
                "winner_model": winner,
                "output": judge_output,
            })

            print(f"  Consensus: {score}% | Winner: {winner} | "
                  f"Trend: {self._trend_str()}")

        return True

    def round_5_synthesis(self) -> Optional[dict]:
        """Synthesis: GLM history summary → 32B final code (all on Pod B)."""
        print(f"\n{'='*60}")
        print(f"Round 5: Synthesis (GLM Summary + 32B Final Code)")
        print(f"{'='*60}\n")
        self.current_round = 5
        self._save_state({"type": "round_start", "phase": "synthesis"})

        state_path = self.state_dir / "state.jsonl"
        full_history = state_path.read_text() if state_path.exists() else ""
        consensus_trend = self._trend_str()

        # ── Step 1: GLM history summary (Pod B) ──
        print(f"  [summary] Switching to GLM-4.7-Flash for history summary (Pod B)...")
        if not self.switch_model(self.summary_model):
            print("  [ERROR] GLM summary switch failed")
            return None
        summary_raw = self.call_llm(
            _build_messages("history_summary", self.summary_model,
                            question=self.question,
                            full_history=full_history[-8000:],
                            consensus_trend=consensus_trend),
            self.summary_model,
        )
        if summary_raw:
            if len(summary_raw) > 3000:
                history_summary = summary_raw[:1798] + "\n...\n" + summary_raw[-1197:]
            else:
                history_summary = summary_raw
        else:
            history_summary = full_history[-3000:]
        self._save_state({"type": "history_summary", "model": self.summary_model,
                          "content": history_summary})

        # ── Step 2: 32B final synthesis (Pod B) ──
        synth_cfg = MODELS[self.synthesizer_model]
        print(f"\n  [synthesis] Loading {synth_cfg['filename']} for 32B final synthesis (Pod B)...")
        print(f"  [synthesis] ctx={synth_cfg['ctx']}, cache={synth_cfg.get('cache_ram',0)}MB")
        if not self.switch_model(self.synthesizer_model):
            print("  [ERROR] 32B failed to load (health timeout or supervisor error)")
            return None

        drag_ctx = self.drag_context or json.dumps({"note": "no DRAG context"})
        final = self.call_llm_json(
            "final_synthesis", self.synthesizer_model,
            question=self.question,
            history_summary=history_summary,
            consensus_trend=consensus_trend,
            drag_context=drag_ctx,
        )
        if not final:
            print(f"  [ERROR] 32B synthesis failed (max_tokens={synth_cfg['max_tokens']}, "
                  f"bench_toks={synth_cfg.get('bench_toks',0.5)}, "
                  f"timeout={int(synth_cfg['max_tokens']/synth_cfg.get('bench_toks',0.5))+300}s)")
            return None

        self._save_state({"type": "final_synthesis",
                          "model": self.synthesizer_model,
                          "output": final})

        print(f"\n  [done] Final synthesis complete: confidence={final.get('confidence', 'N/A')}")
        return final

    # ── Helpers ────────────────────────────────────────────────────────

    def _trend_str(self) -> str:
        if not self.consensus_scores:
            return "(no data)"
        parts = []
        for i, s in enumerate(self.consensus_scores):
            filled = s // 10
            bar = "█" * filled + "░" * (10 - filled)
            parts.append(f"R{i+1}: {bar} {s}%")
        return " | ".join(parts)

    # ═══════════════════════════════════════════════════════════════════
    # Main loop
    # ═══════════════════════════════════════════════════════════════════

    def run_session(self) -> Optional[dict]:
        print(f"\n{'█'*60}")
        print(f"█ DevForge Multi-Agent LLM Debate v3.1 (MoE)")
        print(f"█ Session: {self.session_id}")
        print(f"█ Method: {self.method} | Dry-run: {self.dry_run}")
        print(f"█ Pod B (:8081): GLM → Qwen3-30B → Nemotron → 32B")
        print(f"█ Question: {self.question[:80]}...")
        print(f"{'█'*60}")

        self._save_state({
            "type": "session_start",
            "question": self.question,
            "method": self.method,
            "skip_drag": self.skip_drag,
        })

        # Stop Pod A if running — free its RAM for Pod B model switches
        if not self.dry_run:
            subprocess.run(
                ["systemctl", "--user", "stop", "container-devforge-qwen.service"],
                capture_output=True)
            print("  [pod] Pod A stopped (idle during debate)")
            time.sleep(3)  # brief cooldown for container process exit

        # Round 0: DRAG — Pod B loads GLM for analysis
        self.current_round = 0
        if not self.round_0_drag() and not self.skip_drag:
            print("\n[ABORT] DRAG analysis failed — cannot proceed without context")
            return None

        # Rounds 1-4: DART — Pod B switches P → R → J sequentially
        dart_ok = self.round_1_to_4_dart()

        # Round 5: Synthesis — Pod B loads GLM summary → 32B code
        if not dart_ok:
            print("\n[DART aborted — skipping synthesis]")
        final = self.round_5_synthesis() if dart_ok else None

        # Write report + upload
        if final:
            report_path = self._write_report(final)
            print(f"\n{'█'*60}")
            print(f"█ DEBATE COMPLETE")
            print(f"█ Session: {self.session_id}")
            print(f"█ Confidence: {final.get('confidence', '?')}")
            print(f"█ Local: {report_path}")

            try:
                from lib.blob_uploader import upload_review_bundle
                url = upload_review_bundle(
                    content=report_path.read_text(),
                    pipeline="debate_v3",
                    session_id=self.session_id,
                    metadata={
                        "question": self.question[:100],
                        "method": self.method,
                        "rounds": str(len(self.consensus_scores)),
                        "consensus_trend": self._trend_str(),
                        "confidence": str(final.get("confidence", "?")),
                    },
                )
                print(f"█ Review: {url}")
            except Exception as e:
                print(f"█ Upload skipped: {e}")

            print(f"{'█'*60}")

        return final

    def _write_report(self, final: dict) -> Path:
        path = self.state_dir / "final_report.md"
        lines = [
            f"# Debate Report — {self.session_id}",
            "",
            f"**Question:** {self.question}",
            f"**Method:** {self.method}",
            f"**Rounds:** {len(self.consensus_scores)} debate + synthesis",
            f"**Consensus Trend:** {self._trend_str()}",
            f"**Final Confidence:** {final.get('confidence', '?')}",
            "",
            "## Decision Summary",
            final.get("decision_summary", "(no summary)"),
            "",
            "## Final Diff",
            final.get("diff", "(no diff)"),
            "",
            "## Security Notes",
            *[f"- {n}" for n in final.get("security_notes", [])],
            "",
            "## Performance Notes",
            *[f"- {n}" for n in final.get("performance_notes", [])],
            "",
            "---",
            f"*Generated by DevForge Debate Orchestrator v3.0 (MoE)*",
            f"*Session: {self.session_id}*",
        ]
        path.write_text("\n".join(lines))
        print(f"  [report] {path}")
        return path


def _extract_file_path(question: str) -> Optional[str]:
    """Extract file path from question format: 'File: /path/to/file.py\\nTask: ...'"""
    m = re.search(r"File:\s*(.+?\.py)", question)
    return m.group(1).strip() if m else None


# ═══════════════════════════════════════════════════════════════════════════
# CLI entry
# ═══════════════════════════════════════════════════════════════════════════

def main():
    import argparse
    ap = argparse.ArgumentParser(description="DevForge Multi-Agent LLM Debate v3.0 (MoE)")
    ap.add_argument("--question", "-q", required=True, help="Debate topic (File: ... Task: ...)")
    ap.add_argument("--method", "-m", default="drag",
                    choices=["drag"],
                    help="Debate method (default: drag)")
    ap.add_argument("--skip-drag", action="store_true",
                    help="Skip Round 0 DRAG context analysis")
    ap.add_argument("--dry-run", action="store_true",
                    help="Simulate without actual LLM calls")
    args = ap.parse_args()

    session = DebateSession(
        question=args.question,
        method=args.method,
        skip_drag=args.skip_drag,
        dry_run=args.dry_run,
    )
    result = session.run_session()
    if result is None:
        import sys
        sys.exit(1)


if __name__ == "__main__":
    main()

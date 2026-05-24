#!/usr/bin/env python3
"""DevForge Multi-Agent LLM Debate Orchestrator v2.0

SLOC-exempt: 500+ lines — single cohesive orchestrator coordinating 4 models
across 6 debate rounds (DRAG + DART + Synthesis). Splitting would scatter
shared prompt templates, model configs, and round state across files.

v2.0 — Dual-pod architecture:
  normal:     Pod A :8080 (Qwen3-4B Proposer) + Pod B :8081 (Phi-4-mini Refuter) + Pod B :8082 (Selene Mini Judge)
              All fixed, no switching.

  discussion: Pod A :8080 (Phi-4 14B Judge, fixed) + Pod B :8081 (debate-supervisor, Proposer/Refuter/Synthesizer)
              Overlap: B loads next model while A runs Judge → zero switch overhead.
              R5: stop Pod A, load 32B for final synthesis.

  debate:     Alias for discussion (backward-compatible).

DRAG (Round 0) + DART (Rounds 1-4) + Final Synthesis (Round 5, 32B).

Usage:
  python3 scripts/cli.py discussion drag "How to implement X?"
  python3 scripts/cli.py discussion toolmad "Fix this bug: ..." --skip-drag
  python3 scripts/debate.py --question "..." --method drag [--mode discussion] [--dry-run]
"""
import hashlib
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

# Pod management
POD_A_STOP = "systemctl --user stop container-devforge-qwen.service"
POD_A_START = "systemctl --user start container-devforge-qwen.service"

# ── Model catalogue ────────────────────────────────────────────────────────
# port=8080 → Podman A (fixed, no switching)
# port=8081 → Podman B (debate-supervisor, switching)
# port=8082 → Podman B secondary (fixed, no switching)
MODELS: Dict[str, Dict[str, Any]] = {
    # ── Normal mode models ──
    "qwen3-4b": {
        "filename": "Qwen3-4B-Q4_K_M.gguf",
        "port": 8080, "ctx": 4096, "threads": 4, "mlock": 0,
        "max_tokens": 2048, "temperature": 0.1,
        "system_prompt_support": True,
        "bench_load_s": 30, "bench_toks": 2.5,
    },
    "phi-4-mini": {
        "filename": "Phi-4-mini-instruct.Q8_0.gguf",
        "port": 8081, "ctx": 4096, "threads": 4, "mlock": 0,
        "max_tokens": 2048, "temperature": 0.1,
        "system_prompt_support": True,
        "bench_load_s": 20, "bench_toks": 2.5,
    },
    "selene-mini": {
        "filename": "selene-1-mini-llama-3.1-8b-q4_k_m.gguf",
        "port": 8082, "ctx": 4096, "threads": 4, "mlock": 0,
        "max_tokens": 2048, "temperature": 0.1,
        "system_prompt_support": True,
        "bench_load_s": 30, "bench_toks": 2.0,
    },
    # ── Discussion mode models ──
    "qwen25-coder-14b": {
        "filename": "qwen2.5-coder-14b-instruct-q4_k_m.gguf",
        "port": 8081, "ctx": 4096, "threads": 4, "mlock": 0,
        "max_tokens": 2048, "temperature": 0.1,
        "system_prompt_support": True,
        "bench_load_s": 144, "bench_toks": 2.2,
    },
    "deepcoder-14b": {
        "filename": "agentica-org_DeepCoder-14B-Preview-Q4_K_M.gguf",
        "port": 8081, "ctx": 4096, "threads": 4, "mlock": 1,
        "max_tokens": 2048, "temperature": 0.6, "top_p": 0.95,
        "system_prompt_support": False,
        "bench_load_s": 102, "bench_toks": 2.1,
    },
    "phi-4-14b": {
        "filename": "phi-4-Q4_K_M.gguf",
        "port": 8080, "ctx": 4096, "threads": 4, "mlock": 0,
        "max_tokens": 2048, "temperature": 0.1,
        "system_prompt_support": True,
        "bench_load_s": 40, "bench_toks": 2.1,
    },
    "qwen-32b": {
        "filename": "Qwen2.5-Coder-32B-Instruct-IQ4_XS.gguf",
        "port": 8081, "ctx": 10240, "threads": 4, "mlock": 0,
        "max_tokens": 2048, "temperature": 0.1,
        "system_prompt_support": True,
        "cache_ram": 7168,
        "bench_load_s": 280, "bench_toks": 0.5,
    },
}

# ── Prompt templates ───────────────────────────────────────────────────────

PROMPTS = {
    # ── Round 0: DRAG ──
    "drag_proposer": {
        "system": (
            "You are a search strategist for a code review debate system. "
            "Your job is to propose search queries that will gather objective, "
            "comprehensive information about the topic before any debate begins."
        ),
        "user": (
            "Topic: {question}\n\n"
            "Propose 3 search keywords with a 1-sentence rationale for each. "
            "Consider: official documentation, recent changes, common pitfalls, "
            "security implications, and performance trade-offs.\n\n"
            "Output as JSON:\n"
            '{{"queries": [{{"keyword": "...", "rationale": "..."}}]}}'
        ),
    },
    "drag_refuter": {
        "system": None,  # DeepCoder: no system prompt
        "user": (
            "[ROLE: You are a critical search strategist. Your job is to find bias, "
            "blind spots, and missing perspectives in the proposed search queries.]\n\n"
            "Topic: {question}\n\n"
            "Proposed queries:\n{proposer_queries_json}\n\n"
            "Critically examine these queries:\n"
            "1. What biases do they embed?\n"
            "2. What perspectives are missing?\n"
            "3. Propose 1-2 alternative queries that address these gaps.\n\n"
            "Output as JSON:\n"
            '{{"critique": "2-sentence assessment", '
            '"biases_found": ["bias1", "bias2"], '
            '"alternative_queries": [{{"keyword": "...", "rationale": "..."}}]}}'
        ),
    },
    "drag_judge": {
        "system": (
            "You are a neutral search arbitrator. Your job is to synthesize competing "
            "query proposals into a balanced, comprehensive final query set."
        ),
        "user": (
            "Topic: {question}\n\n"
            "Original queries (Proposer):\n{proposer_queries_json}\n\n"
            "Critique and alternatives (Refuter):\n{refuter_output_json}\n\n"
            "Select 3-5 final search queries that together provide balanced coverage. "
            "For each, explain WHY it was chosen.\n\n"
            "Output as JSON:\n"
            '{{"final_queries": [{{"keyword": "...", "purpose": "..."}}]}}'
        ),
    },

    # ── Rounds 1-4: DART ──
    "dart_proposer": {
        "system": (
            "You are a solution PROPOSER in a code debate. Your role:\n"
            "1. Build on your previous arguments using new evidence.\n"
            "2. Clearly state what you AGREE and DISAGREE with in the refuter's last response.\n"
            "3. Strengthen weak points. Abandon positions that evidence contradicts.\n"
            "4. Cite sources from shared_context where applicable."
        ),
        "user": (
            "Topic: {question}\n\n"
            "History:\n{history_summary}\n\n"
            "Shared Context (search results):\n{shared_context}\n\n"
            "Judge's last assessment: consensus={consensus_score}%, "
            "disagreements={disagreement_points}\n\n"
            "Refuter's last argument:\n{refuter_last_output}\n\n"
            "Your task: Respond with a strengthened proposal.\n"
            "Output STRICT JSON (no extra text):\n"
            '{{"logic_summary": "max 3 sentences", '
            '"code_snippet": "```python\\n...\\n```", '
            '"confidence_score": 0-100, '
            '"disagreement_points": ["point1", "point2"], '
            '"citations": ["source_id_1"]}}'
        ),
    },
    "dart_refuter": {
        "system": None,  # DeepCoder: no system prompt
        "user": (
            "[ROLE: You are a CRITICAL REFUTER in a code debate. Find weaknesses, "
            "propose alternatives, and challenge assumptions. Be constructive — "
            "every critique must come with an alternative suggestion.]\n\n"
            "Topic: {question}\n\n"
            "History:\n{history_summary}\n\n"
            "Shared Context (search results):\n{shared_context}\n\n"
            "Judge's last assessment: consensus={consensus_score}%, "
            "disagreements={disagreement_points}\n\n"
            "Proposer's latest argument:\n{proposer_last_output}\n\n"
            "Your task:\n"
            "1. Identify logical flaws, missing edge cases, or performance issues.\n"
            "2. Propose a concrete alternative for each weakness found.\n"
            "3. Cite sources from shared_context where applicable.\n\n"
            "Output STRICT JSON (no extra text, no markdown wrapper):\n"
            '{{"logic_summary": "max 3 sentences", '
            '"code_snippet": "```python\\n...\\n```", '
            '"confidence_score": 0-100, '
            '"disagreement_points": ["point1", "point2"], '
            '"citations": ["source_id_1"]}}'
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
            "- If consensus < 70%, suggest a search query to resolve the dispute."
        ),
        "user": (
            "Topic: {question}\n\n"
            "Shared Context:\n{shared_context}\n\n"
            "Draft Alpha:\n{proposal_a_anonymized}\n\n"
            "Draft Beta:\n{proposal_b_anonymized}\n\n"
            "Each juror: which draft is stronger and why?\n"
            "Overall consensus score (0-100).\n\n"
            "Output STRICT JSON:\n"
            '{{"jury_opinions": {{"security": "...", "performance": "...", "readability": "..."}}, '
            '"consensus_score": 0-100, '
            '"winner": "alpha|beta|tie", '
            '"suggested_search_query": "keyword or null", '
            '"disagreement_analysis": "1-sentence summary of key unresolved issues"}}'
        ),
    },

    # ── Round 5: Final Synthesis ──
    "history_summary": {
        "system": (
            "You are a debate historian. Condense a multi-round code debate into "
            "a structured 500-word summary suitable for a final judge."
        ),
        "user": (
            "Full Debate History:\n{full_history}\n\n"
            "Consensus Trend: {consensus_trend}\n\n"
            "Summarize in ≤500 words:\n"
            "1. Key arguments from both sides\n"
            "2. Points that were resolved vs. still disputed\n"
            "3. Search findings that influenced the debate\n"
            "4. Final recommendation for the synthesizer\n\n"
            "Output as plain text (no JSON, no markdown)."
        ),
    },
    "final_synthesis": {
        "system": (
            "You are the final synthesizer for a multi-agent code debate. "
            "You have access to the complete debate record and all search results. "
            "The blind is now lifted — you know which model produced which argument. "
            "Your job: produce the definitive, executable final answer."
        ),
        "user": (
            "Topic: {question}\n\n"
            "History Summary:\n{history_summary}\n\n"
            "Last 2 Complete Rounds:\n{last_two_rounds}\n\n"
            "All Search Results:\n{shared_context}\n\n"
            "Consensus Trend: {consensus_trend}\n\n"
            "Produce the final answer:\n"
            "1. Executable final code (complete, not fragmentary).\n"
            "2. Decision summary — why this solution was chosen.\n"
            "3. Security concerns note.\n"
            "4. Performance considerations note.\n\n"
            "Output STRICT JSON:\n"
            '{{"final_code": "```python\\n...\\n```", '
            '"decision_summary": "3-5 sentence reasoning", '
            '"security_notes": ["note1", "note2"], '
            '"performance_notes": ["note1", "note2"], '
            '"confidence": 0-100}}'
        ),
    },
    "final_diff": {
        "system": (
            "You are the final synthesizer for a multi-agent code debate. "
            "You have access to the complete debate record and all search results. "
            "Your job: produce the definitive, executable code modification diff."
        ),
        "user": (
            "Topic: {question}\n\n"
            "History Summary:\n{history_summary}\n\n"
            "Last 2 Complete Rounds:\n{last_two_rounds}\n\n"
            "All Search Results:\n{shared_context}\n\n"
            "Consensus Trend: {consensus_trend}\n\n"
            "Produce the final code modification as a unified diff:\n"
            "1. The diff must be complete and directly applicable (--- / +++ headers, @@ hunks).\n"
            "2. Analysis — why this approach was chosen.\n"
            "3. Rationale — key points that drove the decision.\n\n"
            "Output STRICT JSON:\n"
            '{{"diff": "--- a/file\\n+++ b/file\\n@@ ...", '
            '"analysis": "3-5 sentence reasoning", '
            '"rationale": ["point1", "point2"], '
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
    """Write model-switch.json for supervisor to detect."""
    cfg = MODELS[model_id]
    data = {
        "model_file": cfg["filename"],
        "port": cfg["port"],
        "ctx": cfg["ctx"],
        "threads": cfg["threads"],
        "mlock": cfg["mlock"],
        "cache_ram": cfg.get("cache_ram", 0),
    }
    # Write to host path (bind-mounted inside container)
    os.makedirs(os.path.dirname(SWITCH_FILE), exist_ok=True)
    with open(SWITCH_FILE, "w") as f:
        json.dump(data, f, indent=2)
    print(f"  [switch] wrote {cfg['filename']} (mlock={cfg['mlock']})")


def _poll_health(port: int = 8081, timeout: int = 240, backoff_base: float = 2.0) -> bool:
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


def _dedup_query(query: str, prior_queries: List[str]) -> bool:
    """True if query is a near-duplicate of any prior query (F2)."""
    if not query or query.lower() in ("null", "none", ""):
        return True
    q_words = set(query.lower().split())
    for prior in prior_queries:
        p_words = set(prior.lower().split())
        if not q_words or not p_words:
            continue
        overlap = len(q_words & p_words) / max(len(q_words | p_words), 1)
        if overlap > 0.7:
            return True
    return False


# ═══════════════════════════════════════════════════════════════════════════
# DebateSession
# ═══════════════════════════════════════════════════════════════════════════

class DebateSession:
    """Sequential single-model debate orchestrator.

    Lifecycle:
      1. Write model-switch.json → supervisor hot-swaps → poll health
      2. Call LLM via :8081/v1/chat/completions
      3. Parse JSON response (stdlib + regex fallback)
      4. Repeat for each phase of each round
      5. Persist all state to JSONL for crash recovery
    """

    def __init__(
        self,
        question: str,
        method: str = "drag",
        mode: str = "discussion",
        skip_drag: bool = False,
        dry_run: bool = False,
    ):
        self.question = question
        self.method = method          # "drag" or "toolmad"
        self.mode = mode              # "normal" or "discussion"
        self.skip_drag = skip_drag
        self.dry_run = dry_run

        self.session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.state_dir = SESSIONS_DIR / self.session_id
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.current_round: int = 0
        self.consensus_scores: List[int] = []
        self.shared_context: List[Dict] = []
        self.search_count: int = 0
        self.max_searches: int = 3
        self.winner_map: List[Dict] = []   # F4: round → {alpha, beta, winner}
        self.search_queries: List[str] = []  # F2: dedup history
        self.search_cache: Dict[str, Tuple[List[Dict], float]] = {}  # F7

        # Mode-specific model assignment
        if self.mode == "normal":
            self.proposer_model = "qwen3-4b"
            self.refuter_model = "phi-4-mini"
            self.judge_model = "selene-mini"
            self.synthesizer_model = "selene-mini"
        else:  # discussion
            self.proposer_model = "qwen25-coder-14b"
            self.refuter_model = "deepcoder-14b"
            self.judge_model = "phi-4-14b"
            self.synthesizer_model = "qwen-32b"

        # Neutral history summarizer (always on port 8082 — no switching needed)
        self.summary_model = "selene-mini"

        # Secondary synthesizer for dual-judge comparison (discussion mode)
        self.synthesizer_b_model = "phi-4-14b"

    # ── Persistence ────────────────────────────────────────────────────

    def _save_state(self, entry: Dict) -> None:
        """Append one JSON line to state.jsonl."""
        entry.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        entry.setdefault("round", self.current_round)
        path = self.state_dir / "state.jsonl"
        with open(path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ── Model switching ────────────────────────────────────────────────

    def switch_model(self, model_id: str) -> bool:
        """Ensure model is ready on its port.

        port=8080 (Podman A) → poll health only (fixed model, no switching)
        port=8081 (Podman B) → write switch file + poll (debate-supervisor)
        port=8082 (Podman B secondary) → poll health only (fixed model)
        """
        cfg = MODELS[model_id]
        port = cfg["port"]

        if self.dry_run:
            print(f"  [dry-run] switch to {model_id} ({cfg['filename']}) on :{port}")
            return True

        if port == 8081 and self.mode == "discussion":
            # Podman B — use debate-supervisor for hot-swap
            _write_switch_file(model_id)
            time.sleep(3)  # let supervisor detect change
            return _poll_health(port=port, timeout=cfg.get("bench_load_s", 120) + 60)
        else:
            # Podman A (8080) or Podman B fixed (8082) — already loaded
            return _poll_health(port=port, timeout=10)

    # ── LLM calling ────────────────────────────────────────────────────

    def call_llm(self, messages: List[Dict], model_id: str) -> Optional[str]:
        """Call llama-server and return raw text response. Timeout=300s."""
        cfg = MODELS[model_id]
        port = cfg["port"]
        llm_url = f"http://127.0.0.1:{port}/v1/chat/completions"

        body = {
            "messages": messages,
            "temperature": cfg["temperature"],
            "max_tokens": cfg["max_tokens"],
        }
        if "top_p" in cfg:
            body["top_p"] = cfg["top_p"]

        if self.dry_run:
            print(f"  [dry-run] LLM call :{port}: {len(body['messages'])} msgs, "
                  f"max_tokens={body['max_tokens']}")
            return '{"dry_run": true}'

        print(f"  [llm] calling {model_id} on :{port} (max_tokens={body['max_tokens']})...")
        t_start = time.monotonic()
        try:
            data = json.dumps(body).encode()
            req = urllib.request.Request(
                llm_url, data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=300) as resp:
                result = json.loads(resp.read())
                elapsed = time.monotonic() - t_start
                content = result["choices"][0]["message"]["content"]
                print(f"  [llm] response in {elapsed:.1f}s ({len(content)} chars)")
                return content
        except Exception as e:
            print(f"  [llm] ERROR: {e}")
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

        # Retry: re-prompt with STRICT JSON ONLY
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
            # Final fallback: regex the combined output
            combined = (raw or "") + "\n" + (raw2 or "")
            parsed = _parse_json(combined)

        return parsed

    # ── Web search (stub — real Tavily/SerpAPI integration in Phase 4) ─

    def execute_search(self, queries: List[str]) -> List[Dict]:
        """Execute web searches. Stub for Phase 3 — returns empty in dry-run."""
        results = []
        for q in queries:
            if self.search_count >= self.max_searches:
                print(f"  [search] max ({self.max_searches}) reached, skipping")
                break
            if _dedup_query(q, self.search_queries):
                print(f"  [search] dedup skip: '{q[:60]}...'")
                continue
            if self.dry_run:
                print(f"  [search] dry-run: '{q}'")
                results.append({"query": q, "results": [], "source": "dry-run"})
                continue

            print(f"  [search] query: '{q}'... (stub — Phase 4)")
            results.append({"query": q, "results": [], "source": "stub"})
            self.search_count += 1
            self.search_queries.append(q)
            self._save_state({
                "type": "search_exec", "query": q,
                "results_count": 0, "source": "stub",
            })
        return results

    # ── Anonymize & shuffle (F4/A4) ────────────────────────────────────

    def _shuffle_proposals(
        self, prop_a: dict, prop_b: dict
    ) -> Tuple[dict, dict, str, str]:
        """Randomly assign Alpha/Beta. Log winner map BEFORE shuffle."""
        labels = ["alpha", "beta"]
        if random.random() < 0.5:
            labeled = [("alpha", prop_a), ("beta", prop_b)]
            mapping = {"alpha": self.proposer_model, "beta": self.refuter_model}
        else:
            labeled = [("alpha", prop_b), ("beta", prop_a)]
            mapping = {"alpha": self.refuter_model, "beta": self.proposer_model}

        # Log mapping BEFORE returning (F4: critical ordering)
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
        """Map judge's 'alpha'/'beta' to actual model name."""
        if not self.winner_map:
            return verdict.get("winner", "unknown")
        wm = self.winner_map[-1]
        winner_label = verdict.get("winner", "tie")
        if winner_label in ("alpha", "beta"):
            return wm[winner_label]
        return winner_label

    # ── Early exit ─────────────────────────────────────────────────────

    def _check_early_exit(self) -> Optional[str]:
        """Return reason string if debate should end early, else None."""
        if not self.consensus_scores:
            return None
        latest = self.consensus_scores[-1]
        if latest >= 90:  # Phase 6: calibrate from 95% to 90%
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
        """DRAG: Query Consensus. Returns False if skipped."""
        if self.skip_drag:
            print("\n─── Round 0 (DRAG) SKIPPED (--skip-drag) ───\n")
            self._save_state({"type": "round_skip", "reason": "--skip-drag flag"})
            return False

        # Auto-detect: does this task need search? (simple heuristic for now)
        if any(kw in self.question.lower() for kw in
               ["fix this", "error:", "traceback", ".py:", "debug"]):
            print("\n─── Round 0 (DRAG) SKIPPED (auto-detect: debugging task) ───\n")
            self._save_state({"type": "round_skip", "reason": "auto-detect: debugging task"})
            return False

        print(f"\n{'='*60}")
        print(f"Round 0: DRAG — Query Consensus")
        print(f"{'='*60}\n")
        self._save_state({"type": "round_start", "phase": "drag"})

        # 0.1 — Proposer (Qwen2.5-Coder-14B)
        if not self.switch_model(self.proposer_model):
            return False
        proposer = self.call_llm_json(
            "drag_proposer", self.proposer_model,
            question=self.question,
        )
        if not proposer:
            print("  [ERROR] proposer failed")
            return False
        self._save_state({"type": "llm_response", "phase": "drag_proposer",
                          "model": self.proposer_model, "output": proposer})

        # 0.2 — Refuter (DeepCoder-14B)
        if not self.switch_model(self.refuter_model):
            return False
        refuter = self.call_llm_json(
            "drag_refuter", self.refuter_model,
            question=self.question,
            proposer_queries_json=json.dumps(proposer, indent=2),
        )
        if not refuter:
            print("  [ERROR] refuter failed")
            return False
        self._save_state({"type": "llm_response", "phase": "drag_refuter",
                          "model": self.refuter_model, "output": refuter})

        # 0.3 — Judge (Phi-4-14B)
        if not self.switch_model(self.judge_model):
            return False
        judge = self.call_llm_json(
            "drag_judge", self.judge_model,
            question=self.question,
            proposer_queries_json=json.dumps(proposer, indent=2),
            refuter_output_json=json.dumps(refuter, indent=2),
        )
        if not judge:
            print("  [ERROR] judge failed")
            return False
        self._save_state({"type": "llm_response", "phase": "drag_judge",
                          "model": self.judge_model, "output": judge})

        # 0.4 — Execute search
        queries = [q["keyword"] for q in judge.get("final_queries", [])]
        print(f"\n  Selected {len(queries)} queries for search")
        search_results = self.execute_search(queries)
        self.shared_context.extend(search_results)
        with open(self.state_dir / "shared_context.json", "w") as f:
            json.dump(self.shared_context, f, indent=2, ensure_ascii=False)

        print(f"\n  Round 0 complete: {len(search_results)} searches executed")
        return True

    def round_1_to_4_dart(self) -> bool:
        """DART: Rounds 1-4 — Proposer → Refuter → Judge → DART check."""
        proposer_output = None
        refuter_output = None
        last_disagreement = "N/A (first round)"

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

            # Build history summary
            history_summary = json.dumps({
                "round": rnd,
                "prior_consensus": self.consensus_scores,
                "shared_context_count": len(self.shared_context),
            })

            # A — Proposer (Qwen2.5-Coder-14B)
            if not self.switch_model(self.proposer_model):
                continue
            proposer_output = self.call_llm_json(
                "dart_proposer", self.proposer_model,
                question=self.question,
                history_summary=history_summary,
                shared_context=json.dumps(self.shared_context, indent=2),
                consensus_score=str(self.consensus_scores[-1] if self.consensus_scores else "N/A"),
                disagreement_points=last_disagreement,
                refuter_last_output=json.dumps(refuter_output, indent=2) if refuter_output else "N/A (first round)",
            )
            if not proposer_output:
                print("  [ERROR] proposer failed")
                continue
            self._save_state({"type": "llm_response", "phase": "dart_proposer",
                              "model": self.proposer_model, "output": proposer_output})

            # B — Refuter (DeepCoder-14B)
            if not self.switch_model(self.refuter_model):
                continue
            refuter_output = self.call_llm_json(
                "dart_refuter", self.refuter_model,
                question=self.question,
                history_summary=history_summary,
                shared_context=json.dumps(self.shared_context, indent=2),
                consensus_score=str(self.consensus_scores[-1] if self.consensus_scores else "N/A"),
                disagreement_points=last_disagreement,
                proposer_last_output=json.dumps(proposer_output, indent=2),
            )
            if not refuter_output:
                print("  [ERROR] refuter failed")
                continue
            self._save_state({"type": "llm_response", "phase": "dart_refuter",
                              "model": self.refuter_model, "output": refuter_output})

            # C — Judge (Phi-4-14B) + Winner Mapping
            if not self.switch_model(self.judge_model):
                continue
            alpha, beta, _, _ = self._shuffle_proposals(proposer_output, refuter_output)
            judge_output = self.call_llm_json(
                "dart_judge", self.judge_model,
                question=self.question,
                shared_context=json.dumps(self.shared_context, indent=2),
                proposal_a_anonymized=json.dumps(alpha, indent=2),
                proposal_b_anonymized=json.dumps(beta, indent=2),
            )
            if not judge_output:
                print("  [ERROR] judge failed")
                continue

            winner = self._resolve_winner(judge_output)
            self._save_state({
                "type": "judge_verdict",
                "consensus_score": judge_output.get("consensus_score", 0),
                "winner_label": judge_output.get("winner"),
                "winner_model": winner,
                "dart_triggered": False,
                "output": judge_output,
            })

            score = judge_output.get("consensus_score", 0)
            self.consensus_scores.append(score)
            last_disagreement = judge_output.get("disagreement_analysis", "no specific disagreements")
            print(f"  Consensus: {score}% | Winner: {winner} | "
                  f"Trend: {self._trend_str()}")

            # D — DART Trigger Check (with dedup + null guard, FIX-3)
            query = judge_output.get("suggested_search_query")
            if (score < 70 and self.search_count < self.max_searches
                    and query and str(query).lower() not in ("null", "none", "")):
                if not _dedup_query(str(query), self.search_queries):
                    print(f"  DART trigger: searching '{query}'")
                    results = self.execute_search([str(query)])
                    self.shared_context.extend(results)
                    self._save_state({
                        "type": "dart_trigger",
                        "query": query,
                        "results_count": len(results),
                    })
                else:
                    print(f"  DART trigger: dedup skip '{query}'")

        return True

    def round_5_synthesis(self) -> Optional[dict]:
        """Dual-judge synthesis: Selene (8082) summary → 32B + Phi-4-14B (8081).

        Selene Mini on 8082 is always running — no switching needed.
        32B and Phi-4-14B share 8081 sequentially with switch_model().
        Returns {"32b": {...}, "phi4": {...}} for external comparison.
        """
        print(f"\n{'='*60}")
        print(f"Round 5: Final Synthesis (Dual Judge)")
        print(f"{'='*60}\n")
        self.current_round = 5
        self._save_state({"type": "round_start", "phase": "final_synthesis"})

        # Build full history from state file
        state_path = self.state_dir / "state.jsonl"
        full_history = ""
        if state_path.exists():
            full_history = state_path.read_text()

        consensus_trend = self._trend_str()

        # ── Step 1: Selene Mini history summary (neutral 3rd party, port 8082) ──
        summary_model = self.summary_model
        print(f"  Generating history summary with {summary_model} (neutral, :8082)...")
        summary_raw = self.call_llm(
            _build_messages("history_summary", summary_model,
                            full_history=full_history[-8000:],
                            consensus_trend=consensus_trend),
            summary_model,
        )
        if summary_raw:
            if len(summary_raw) > 3000:
                history_summary = summary_raw[:1798] + "\n...\n" + summary_raw[-1197:]
            else:
                history_summary = summary_raw
        else:
            history_summary = full_history[-3000:]
        self._save_state({"type": "history_summary", "model": summary_model,
                          "content": history_summary})

        # Build last 2 rounds from JSONL
        last_two = ""
        if state_path.exists():
            lines = state_path.read_text().strip().split("\n")
            dart_lines = [l for l in lines if '"phase": "dart_' in l.lower() or 'dart_' in l.lower()]
            last_two = "\n".join(dart_lines[-6:])

        # ── Step 2: 32B synthesis A (port 8081) ──
        synth_a_model = self.synthesizer_model      # "qwen-32b"
        synth_b_model = self.synthesizer_b_model    # "phi-4-14b"

        synth_a_name = MODELS[synth_a_model]["filename"]
        print(f"\n  [Synth A] Loading {synth_a_name} for 32B synthesis...")
        if not self.switch_model(synth_a_model):
            return None

        synth_a = self.call_llm_json(
            "final_diff", synth_a_model,
            question=self.question,
            history_summary=history_summary,
            last_two_rounds=last_two or "(see history summary)",
            shared_context=json.dumps(self.shared_context, indent=2),
            consensus_trend=consensus_trend,
        )
        if not synth_a:
            print("  [ERROR] 32B synthesis failed")
            synth_a = {"error": "synthesis failed"}

        self._save_state({
            "type": "final_synthesis",
            "model": synth_a_model,
            "judge": "A",
            "output": synth_a,
        })

        # ── Step 3: Phi-4-14B synthesis B (port 8081) ──
        synth_b_name = MODELS[synth_b_model]["filename"]
        print(f"\n  [Synth B] Loading {synth_b_name} for Phi-4-14B synthesis...")
        if not self.switch_model(synth_b_model):
            synth_b = {"error": "model switch failed"}
        else:
            synth_b = self.call_llm_json(
                "final_diff", synth_b_model,
                question=self.question,
                history_summary=history_summary,
                last_two_rounds=last_two or "(see history summary)",
                shared_context=json.dumps(self.shared_context, indent=2),
                consensus_trend=consensus_trend,
            )
            if not synth_b:
                print("  [ERROR] Phi-4-14B synthesis failed")
                synth_b = {"error": "synthesis failed"}

        self._save_state({
            "type": "final_synthesis",
            "model": synth_b_model,
            "judge": "B",
            "output": synth_b,
        })

        result = {"32b": synth_a, "phi4": synth_b}
        print(f"\n  [done] Dual synthesis complete: "
              f"32B={synth_a.get('confidence', 'N/A')}, "
              f"Phi-4={synth_b.get('confidence', 'N/A')}")
        return result

    # ── Helpers ────────────────────────────────────────────────────────

    def _trend_str(self) -> str:
        """ASCII bar chart of consensus trend (F8)."""
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
        """Execute full debate session. Returns final output or None on failure."""
        print(f"\n{'█'*60}")
        print(f"█ DevForge Multi-Agent LLM Debate v2.0")
        print(f"█ Session: {self.session_id}")
        print(f"█ Mode: {self.mode} | Method: {self.method} | Dry-run: {self.dry_run}")
        print(f"█ Models: P={self.proposer_model} R={self.refuter_model} J={self.judge_model} S={self.synthesizer_model}")
        print(f"█ Question: {self.question[:80]}...")
        print(f"{'█'*60}")

        self._save_state({
            "type": "session_start",
            "question": self.question,
            "method": self.method,
            "skip_drag": self.skip_drag,
        })

        # Round 0: DRAG
        self.current_round = 0
        self.round_0_drag()

        # Rounds 1-4: DART
        self.round_1_to_4_dart()

        # Round 5: Final Synthesis
        final = self.round_5_synthesis()

        # Restore Podman A if stopped during discussion R5
        if self.mode == "discussion" and not self.dry_run:
            print("  [pod] restarting Podman A for next session...")
            subprocess.run(POD_A_START.split(), capture_output=True)
            time.sleep(2)
            print("  [pod] Podman A restarted")

        # Write report + upload to Azure Blob
        if final:
            report_path = self._write_report(final)
            print(f"\n{'█'*60}")
            print(f"█ DEBATE COMPLETE")
            print(f"█ Session: {self.session_id}")
            if isinstance(final, dict) and "32b" in final:
                c32 = final["32b"].get("confidence", "?")
                cp4 = final["phi4"].get("confidence", "?")
                print(f"█ 32B confidence: {c32}  |  Phi-4 confidence: {cp4}")
            else:
                print(f"█ Confidence: {final.get('confidence', '?')}")
            print(f"█ Local: {report_path}")

            # Auto-upload review bundle to Azure Blob
            try:
                from lib.blob_uploader import upload_review_bundle
                url = upload_review_bundle(
                    content=report_path.read_text(),
                    pipeline="debate",
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
        """Generate final_report.md. Returns path."""
        path = self.state_dir / "final_report.md"
        lines = [
            f"# Debate Report — {self.session_id}",
            "",
            f"**Question:** {self.question}",
            f"**Method:** {self.method}",
            f"**Rounds:** {len(self.consensus_scores)} debate + synthesis",
            f"**Consensus Trend:** {self._trend_str()}",
            f"**Final Confidence:** {final.get('confidence', '?')}%",
            "",
            "## Decision Summary",
            final.get("decision_summary", "(no summary)"),
            "",
            "## Final Code",
            final.get("final_code", "(no code)"),
            "",
            "## Security Notes",
            *[f"- {n}" for n in final.get("security_notes", [])],
            "",
            "## Performance Notes",
            *[f"- {n}" for n in final.get("performance_notes", [])],
            "",
            "---",
            f"*Generated by DevForge Debate Orchestrator v2.0*",
            f"*Session: {self.session_id}*",
        ]
        path.write_text("\n".join(lines))
        print(f"  [report] {path}")
        return path


# ═══════════════════════════════════════════════════════════════════════════
# CLI entry
# ═══════════════════════════════════════════════════════════════════════════

def main():
    import argparse
    ap = argparse.ArgumentParser(description="DevForge Multi-Agent LLM Debate v2.0")
    ap.add_argument("--question", "-q", required=True, help="Debate topic")
    ap.add_argument("--method", "-m", default="drag",
                    choices=["drag", "toolmad"],
                    help="Debate method (default: drag)")
    ap.add_argument("--mode", default="discussion",
                    choices=["normal", "discussion", "debate"],
                    help="Mode: normal (3 fixed models) or discussion (dual-pod overlap). "
                         "'debate' is an alias for 'discussion'.")
    ap.add_argument("--skip-drag", action="store_true",
                    help="Skip Round 0 DRAG query consensus")
    ap.add_argument("--dry-run", action="store_true",
                    help="Simulate without actual LLM calls")
    args = ap.parse_args()

    # 'debate' is backward-compatible alias for 'discussion'
    mode = "discussion" if args.mode == "debate" else args.mode

    session = DebateSession(
        question=args.question,
        method=args.method,
        mode=mode,
        skip_drag=args.skip_drag,
        dry_run=args.dry_run,
    )
    result = session.run_session()
    if result is None:
        sys.exit(1)


if __name__ == "__main__":
    import sys
    main()

#!/usr/bin/env python3
"""DevForge Multi-Agent LLM Debate Orchestrator v4.6

Multi-mode debate — debate (local Pod A+B) or cooperative (spot VMs + local).
No 32B — batch pipeline only.

Modes:
  debate:      Pod A (:8080) Judge + Pod B (:8081) sequential (14B+MoE local)
  cooperative: provision spot VMs from golden images → Proposer(Qwen3-30B) + Refuter(Nemotron-3-Nano) ×4 rounds
               → terminate VMs → Judge/DRAG/Summary/Synthesis on local Pod B

Usage:
  python3 scripts/debate.py --question "File: ...\nTask: ..." [--mode cooperative] [--skip-drag] [--dry-run]
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

import sys as _sys
_scripts_dir = str(Path(__file__).resolve().parent)
if _scripts_dir not in _sys.path:
    _sys.path.insert(0, _scripts_dir)
from azure_spot import (
    QWEN_SPOT_CONFIG,
    NEMOTRON_SPOT_CONFIG,
    SpotOrchestrator,
    open_spot_tunnel,
    close_spot_tunnel,
)

# ── Paths ──────────────────────────────────────────────────────────────────
SWITCH_FILE = "/opt/ai_data/debate/switch/model-switch.json"
SESSIONS_DIR = Path("/opt/ai_data/debate_sessions")

# ── Model catalogue (MoE lineup) ──────────────────────────────────────────
MODELS: Dict[str, Dict[str, Any]] = {
    # GLM-4.7-Flash — standby (not used in 14B lineup, kept for fallback)
    # enable_thinking=false required: thinking tokens consume max_tokens budget leaving empty content
    "glm-47-flash": {
        "filename": "GLM-4.7-Flash-Q4_K_M.gguf",
        "port": 8081, "ctx": 4096, "threads": 4, "mlock": 0,
        "max_tokens": 2048, "temperature": 0.1,
        "system_prompt_support": True,
        "bench_load_s": 480, "bench_toks": 2.8,
        "cache_ram": 1024,
        "chat_template_kwargs": {"enable_thinking": False},
    },
    # Remote spot model — Azure (SSH-tunneled to OpenAI-compatible API, always-on)
    "qwen3-30b-a3b": {
        "filename": "Qwen3-30B-A3B-Q4_K_M.gguf",
        "host": "azureqwen", "port": 400, "local_port": 8084,
        "model_name": "qwen",  # model name on remote API
        "ctx": 4096, "threads": 2, "mlock": 0,
        "max_tokens": 1024, "temperature": 0.1,
        "system_prompt_support": True,
        "bench_load_s": 10, "bench_toks": 6.0,
        "cache_ram": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    },
    # Remote spot model — Azure Nemotron-3-Nano-30B-A3B MoE (SSH-tunneled)
    "nemotron3-nano-30b": {
        "filename": "Nemotron-3-Nano-30B-A3B-IQ4_XS.gguf",
        "host": "azurenemo", "port": 400, "local_port": 8083,
        "model_name": "nemotron",
        "ctx": 4096, "threads": 2, "mlock": 0,
        "max_tokens": 1024, "temperature": 0.6, "top_p": 0.95,
        "system_prompt_support": True,
        "bench_load_s": 10, "bench_toks": 5.5,
        "cache_ram": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    },
    # 32B — batch pipeline only, excluded from debate
    "qwen-32b": {
        "filename": "Qwen2.5-Coder-32B-Instruct-IQ4_XS.gguf",
        "port": 8081, "ctx": 4096, "threads": 4, "mlock": 0,
        "max_tokens": 1024, "temperature": 0.1,
        "system_prompt_support": True,
        "cache_ram": 2048,
        "bench_load_s": 480, "bench_toks": 0.5,
    },
    # 14B class — lighter local models for debate roles
    "qwen-14b": {
        "filename": "Qwen2.5-Coder-14B-Instruct-Q4_K_M.gguf",
        "port": 8081, "ctx": 4096, "threads": 4, "mlock": 0,
        "max_tokens": 1024, "temperature": 0.1,
        "system_prompt_support": True,
        "cache_ram": 1024,
        "bench_load_s": 240, "bench_toks": 3.0,
    },
    "deepseek-v2-lite": {
        "filename": "DeepSeek-Coder-V2-Lite-Instruct-Q4_K_M.gguf",
        "port": 8081, "ctx": 4096, "threads": 4, "mlock": 0,
        "max_tokens": 1024, "temperature": 0.1,
        "system_prompt_support": True,
        "cache_ram": 1024,
        "bench_load_s": 280, "bench_toks": 8.0,
    },
    "starcoder2-15b": {
        "filename": "starcoder2-15b-instruct-Q4_K_M.gguf",
        "port": 8081, "ctx": 4096, "threads": 4, "mlock": 0,
        "max_tokens": 1024, "temperature": 0.3,
        "system_prompt_support": True,
        "cache_ram": 1024,
        "bench_load_s": 300, "bench_toks": 5.0,
    },
    # MoE refuter — small active params, fast inference
    "phi-mini-moe": {
        "filename": "Phi-mini-MoE-instruct-Q4_K_M.gguf",
        "port": 8081, "ctx": 4096, "threads": 4, "mlock": 0,
        "max_tokens": 1024, "temperature": 0.4,
        "system_prompt_support": True,
        "cache_ram": 512,
        "bench_load_s": 140, "bench_toks": 10.0,
    },
    # Pod A (:8080) — always-on Qwen3-4B Judge
    "qwen3-4b": {
        "filename": "Qwen3-4B-Q4_K_M.gguf",
        "port": 8080, "ctx": 4096, "threads": 4, "mlock": 0,
        "max_tokens": 1024, "temperature": 0.1,
        "system_prompt_support": True,
        "cache_ram": 256,
        "bench_load_s": 30, "bench_toks": 5.0,
    },
    # 8B judge — lightweight, proven in normal mode (backup)
    "selene-mini": {
        "filename": "selene-1-mini-llama-3.1-8b-q4_k_m.gguf",
        "port": 8080, "ctx": 4096, "threads": 4, "mlock": 0,
        "max_tokens": 1024, "temperature": 0.1,
        "system_prompt_support": True,
        "cache_ram": 512,
        "bench_load_s": 90, "bench_toks": 5.0,
    },
}

# ── Remote host catalogue ─────────────────────────────────────────────────
REMOTE_HOSTS: Dict[str, Dict[str, str]] = {
    "azureqwen": {
        "ssh_host": "azureqwen",
        "model_dir": "/models",
        "description": "Azure spot VM — Qwen3-30B-A3B, 41GB RAM, 2-core x86_64",
    },
    "azurenemo": {
        "ssh_host": "azurenemo",
        "model_dir": "/models",
        "description": "Azure spot VM — Nemotron-3-Nano-30B-A3B MoE, 41GB RAM, 2-core x86_64",
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


# ── Remote spot model management ──────────────────────────────────────────

def _kill_ssh_tunnel(local_port: int) -> None:
    """Kill SSH tunnel process bound to local_port. Only kills ssh processes."""
    try:
        result = subprocess.run(
            ["ss", "-tlnp"], capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            if f":{local_port}" in line and "ssh" in line.lower():
                m = re.search(r"pid=(\d+)", line)
                if m:
                    pid = int(m.group(1))
                    try:
                        os.kill(pid, 15)
                        print(f"  [tunnel] Closed :{local_port} (pid={pid})")
                    except Exception:
                        pass
    except Exception:
        pass


def _remote_activate(model_id: str, session_tunnels: set) -> bool:
    """Ensure remote model is accessible via SSH tunnel.

    Tunnel is opened once per session and reused across rounds.
    Model is always-on (OpenAI-compatible API on remote).
    """
    cfg = MODELS[model_id]
    host = cfg["host"]
    rh = REMOTE_HOSTS[host]
    ssh_host = rh["ssh_host"]
    local_port = cfg["local_port"]
    remote_port = cfg["port"]

    # Tunnel already open — just verify health
    if host in session_tunnels:
        if _poll_health(port=local_port, timeout=3):
            print(f"  [tunnel] Reusing :{local_port} -> {host}:{remote_port}")
            return True
        # Tunnel died — clean up and re-open
        _kill_ssh_tunnel(local_port)
        session_tunnels.discard(host)

    # Open SSH tunnel
    print(f"  [tunnel] Opening :{local_port} -> {host}:{remote_port}")
    try:
        tunnel_result = subprocess.run(
            [
                "ssh", "-f", "-N",
                "-o", "StrictHostKeyChecking=no",
                "-o", "ExitOnForwardFailure=yes",
                "-o", "ServerAliveInterval=60",
                "-o", "ServerAliveCountMax=3",
                "-L", f"{local_port}:localhost:{remote_port}",
                ssh_host,
            ],
            capture_output=True, timeout=10,
        )
        if tunnel_result.returncode != 0:
            print(f"  [tunnel] ERROR: {tunnel_result.stderr.decode()}")
            return False
    except subprocess.TimeoutExpired:
        print(f"  [tunnel] ERROR: SSH tunnel timeout to {host}")
        return False
    except Exception as e:
        print(f"  [tunnel] ERROR: SSH tunnel failed to {host}: {e}")
        return False

    time.sleep(1)
    timeout = cfg.get("bench_load_s", 10) + 5
    if not _poll_health(port=local_port, timeout=timeout):
        print(f"  [remote] ERROR: health check failed through tunnel :{local_port}")
        return False
    session_tunnels.add(host)
    return True


def _remote_deactivate(model_id: str) -> None:
    """Deactivate spot model — kill SSH tunnel only (model stays running on remote)."""
    cfg = MODELS[model_id]
    host = cfg.get("host", "local")
    if host == "local":
        return

    local_port = cfg["local_port"]
    _kill_ssh_tunnel(local_port)
    print(f"  [remote] Deactivated {model_id} tunnel :{local_port}")


# ═══════════════════════════════════════════════════════════════════════════
# DebateSession
# ═══════════════════════════════════════════════════════════════════════════

class DebateSession:
    """Multi-agent debate orchestrator with mode-based model assignment.

    Modes:
      debate (default): Pod A (:8080) Judge + Pod B (:8081) sequential.
        DRAG/Summary → Qwen-14B, Proposer/Synthesis → DeepSeek-V2-Lite, Refuter → Phi-mini-MoE.
      cooperative: provision spot VMs → Proposer(Qwen3-30B) + Refuter(Nemotron-3-Nano) ×4 rounds
        → terminate VMs → Judge/DRAG/Summary/Synthesis on local Pod B.

    No 32B — batch pipeline only.
    """

    def __init__(
        self,
        question: str,
        method: str = "drag",
        skip_drag: bool = False,
        dry_run: bool = False,
        mode: str = "debate",
    ):
        self.question = question
        self.method = method
        self.skip_drag = skip_drag
        self.dry_run = dry_run
        self.mode = mode

        self.session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.state_dir = SESSIONS_DIR / self.session_id
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.current_round: int = 0
        self.consensus_scores: List[int] = []
        self.winner_map: List[Dict] = []
        self.drag_context: str = ""
        self._tunnels_open: set = set()  # Track active SSH tunnels
        self._spot_orch: Optional[SpotOrchestrator] = None  # Spot VM orchestrator
        self._spot_tunnel_ports: List[int] = []  # Local ports for spot tunnels

        if mode == "cooperative":
            # Azure: Proposer(Qwen3-30B) + Refuter(Nemotron-3-Nano), rest local Podcast B
            self.drag_model = "deepseek-v2-lite"        # local Pod B — DRAG analysis
            self.proposer_model = "qwen3-30b-a3b"       # azureqwen :8084 — Proposer
            self.refuter_model = "nemotron3-nano-30b"   # azurenemo :8083 — Refuter
            self.judge_model = "glm-47-flash"           # local Pod B — Judge (glm4moe)
            self.summary_model = "deepseek-v2-lite"      # local Pod B — History summary
            self.synthesizer_model = "qwen-14b"          # local Pod B — Final synthesis
        else:
            # Default debate mode: Pod A Judge + Pod B sequential
            self.drag_model = "qwen-14b"                # Pod B — DRAG analysis
            self.proposer_model = "deepseek-v2-lite"    # Pod B — Proposer
            self.refuter_model = "phi-mini-moe"         # Pod B — Refuter
            self.judge_model = "selene-mini"             # Pod A :8080 — always-on
            self.summary_model = "qwen-14b"             # Pod B — History summary
            self.synthesizer_model = "deepseek-v2-lite"  # Pod B — Final synthesis

    # ── Persistence ────────────────────────────────────────────────────

    def _save_state(self, entry: Dict) -> None:
        entry.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        entry.setdefault("round", self.current_round)
        path = self.state_dir / "state.jsonl"
        with open(path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ── Model switching ────────────────────────────────────────────────

    def _close_all_tunnels(self) -> None:
        """Close all SSH tunnels opened during this session."""
        for host in list(self._tunnels_open):
            for model_id, cfg in MODELS.items():
                if cfg.get("host") == host:
                    _kill_ssh_tunnel(cfg["local_port"])
                    break
        print(f"  [tunnel] Session tunnels closed ({len(self._tunnels_open)} were open)")
        self._tunnels_open.clear()

    # ── Spot VM lifecycle ──────────────────────────────────────────────

    def _provision_spot_vms(self) -> bool:
        """Create spot VMs for Proposer (Qwen) and Refuter (Nemotron)."""
        print(f"\n{'─'*40}")
        print(f"  [spot] Provisioning spot VMs from golden images...")
        print(f"{'─'*40}")

        if self.dry_run:
            print("  [spot] DRY RUN — would create: qwen3-30b + nemotron3-nano spot VMs")
            return True

        try:
            orch = SpotOrchestrator(self.session_id)
            orch.add("qwen", QWEN_SPOT_CONFIG)
            orch.add("nemotron", NEMOTRON_SPOT_CONFIG)
            if not orch.provision_all():
                print("  [spot] ERROR: Spot VM provisioning failed")
                return False
            self._spot_orch = orch
            return True
        except Exception as e:
            print(f"  [spot] ERROR: {e}")
            return False

    def _open_spot_tunnels(self) -> bool:
        """Open SSH tunnels to spot VM IPs on the same local ports."""
        if self.dry_run:
            print("  [spot] DRY RUN — would open tunnels to spot VMs")
            return True

        if not self._spot_orch:
            print("  [spot] No orchestrator — cannot open tunnels")
            return False

        ssh_keys = {
            "qwen": os.path.expanduser("~/.ssh/vm-azure-qwen-30B-A3B-key.pem"),
            "nemotron": os.path.expanduser("~/.ssh/vm-azure-nvidia-nemotron3-nano-30B-key.pem"),
        }
        tunnel_map = {
            "qwen": ("qwen3-30b-a3b", 8084, "azureqwen"),
            "nemotron": ("nemotron3-nano-30b", 8083, "azurenemo"),
        }

        for label, (model_key, local_port, host_name) in tunnel_map.items():
            mgr = self._spot_orch.managers.get(label)
            if not mgr or not mgr.cfg.public_ip:
                print(f"  [spot] No IP for {label} — skipping tunnel")
                return False

            key_path = ssh_keys[label]
            ip = mgr.cfg.public_ip
            if not open_spot_tunnel(label, ip, 400, local_port, key_path):
                print(f"  [spot] Tunnel failed for {label}")
                return False
            self._spot_tunnel_ports.append(local_port)
            self._tunnels_open.add(host_name)  # Match MODELS host key for reuse detection

        return True

    def _terminate_spot_vms(self) -> None:
        """Close spot tunnels and delete all spot VMs."""
        for port in self._spot_tunnel_ports:
            close_spot_tunnel(port)
        self._spot_tunnel_ports.clear()

        if self._spot_orch:
            print(f"\n{'─'*40}")
            print(f"  [spot] Terminating spot VMs...")
            print(f"{'─'*40}")
            self._spot_orch.terminate_all()
            self._spot_orch = None
        elif not self.dry_run:
            print("  [spot] No spot VMs to terminate")

    def switch_model(self, model_id: str) -> bool:
        """Ensure model is ready — local supervisor or remote spot activation."""
        cfg = MODELS[model_id]
        host = cfg.get("host", "local")

        if self.dry_run:
            loc = f"remote {host}:{cfg.get('local_port', cfg['port'])}" if host != "local" else f":{cfg['port']}"
            print(f"  [dry-run] switch to {model_id} ({cfg['filename']}) on {loc}")
            return True

        if host == "local":
            port = cfg["port"]
            if port == 8080:
                # Pod A — always-on persistent model, just verify health
                return _poll_health(port=8080, timeout=cfg.get("bench_load_s", 30) + 30)
            # Pod B (:8081) — write switch file for supervisor
            _write_switch_file(model_id)
            # Wait for supervisor to detect the change and kill old server
            # (supervisor polls every 5s; old server must die before we poll health)
            time.sleep(5)
            for _ in range(12):  # wait up to 60s for old server to go down
                try:
                    req = urllib.request.Request(f"http://127.0.0.1:8081/health")
                    with urllib.request.urlopen(req, timeout=3):
                        pass
                    time.sleep(5)  # old server still up — supervisor hasn't acted yet
                except Exception:
                    break  # old server down — supervisor is switching
            return _poll_health(port=8081, timeout=cfg.get("bench_load_s", 120) + 60)
        else:
            return _remote_activate(model_id, self._tunnels_open)

    # ── LLM calling ────────────────────────────────────────────────────

    def call_llm(self, messages: List[Dict], model_id: str, max_tokens: Optional[int] = None) -> Optional[str]:
        """Call llama-server and return raw text response.

        Timeout = max_tokens / bench_toks + 300s buffer.
        MoE models at ~4-10 tok/s, 32B at ~0.5 tok/s.
        On connection error, retries once with halved max_tokens.
        """
        cfg = MODELS[model_id]
        port = cfg.get("local_port", cfg["port"])
        llm_url = f"http://127.0.0.1:{port}/v1/chat/completions"

        mt = max_tokens if max_tokens is not None else cfg["max_tokens"]
        body: Dict[str, Any] = {
            "messages": messages,
            "temperature": cfg["temperature"],
            "max_tokens": mt,
        }
        if "model_name" in cfg:
            body["model"] = cfg["model_name"]
        if "top_p" in cfg:
            body["top_p"] = cfg["top_p"]
        if "chat_template_kwargs" in cfg:
            body["chat_template_kwargs"] = cfg["chat_template_kwargs"]

        if self.dry_run:
            print(f"  [dry-run] LLM call :{port}: {len(body['messages'])} msgs, "
                  f"max_tokens={body['max_tokens']}")
            return '{"dry_run": true}'

        for attempt in range(2):
            gen_rate = cfg.get("bench_toks", 2.0)
            timeout = int(body["max_tokens"] / gen_rate) + 600
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
                    content = result["choices"][0]["message"]["content"] or ""
                    if not content.strip():
                        reasoning = result["choices"][0]["message"].get("reasoning_content", "")
                        if reasoning:
                            content = reasoning
                            print(f"  [llm] response in {elapsed:.1f}s ({len(content)} chars, from reasoning_content)")
                        else:
                            print(f"  [llm] response in {elapsed:.1f}s (0 chars, empty)")
                            if attempt == 0 and body["max_tokens"] > 256:
                                body["max_tokens"] = max(body["max_tokens"] // 2, 256)
                                body["temperature"] = min(body["temperature"], 0.1)
                                print(f"  [llm] retrying with max_tokens={body['max_tokens']}...")
                                time.sleep(3)
                                continue
                            return None
                    else:
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
        """DRAG: Qwen-14B analyzes target file and sets debate context.

        Pod B (:8081) loads Qwen2.5-Coder-14B for the first time.
        Returns False if skipped.
        """
        if self.skip_drag:
            print("\n─── Round 0 (DRAG) SKIPPED (--skip-drag) ───\n")
            self._save_state({"type": "round_skip", "reason": "--skip-drag flag"})
            # Load drag model for summary role later
            if not self.switch_model(self.drag_model):
                print("  [ERROR] Drag model failed to load on Pod B (skip_drag path)")
                return False
            return False

        print(f"\n{'='*60}")
        print(f"Round 0: DRAG — Context Analysis (Qwen2.5-Coder-14B)")
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

        Proposer/Refuter run on Pod B (:8081) sequentially.
        Judge runs on Pod A (:8080) — always-on, no switch needed.

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

            # A — Proposer (Pod B: Qwen2.5-Coder-14B)
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

            # B — Refuter (Pod B: Phi-mini-MoE)
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

            # C — Judge (Pod B: DeepSeek-Coder-V2-Lite)
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
        """Synthesis: MoE history summary + final code (all on Pod B, no 32B)."""
        print(f"\n{'='*60}")
        print(f"Round 5: Synthesis (DeepSeek Summary + Synthesis)")
        print(f"{'='*60}\n")
        self.current_round = 5
        self._save_state({"type": "round_start", "phase": "synthesis"})

        state_path = self.state_dir / "state.jsonl"
        full_history = state_path.read_text() if state_path.exists() else ""
        consensus_trend = self._trend_str()

        # ── Step 1: GLM history summary (Pod B) ──
        print(f"  [summary] Switching to DeepSeek-Coder-V2-Lite for history summary (Pod B)...")
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

        # ── Step 2: MoE final synthesis (Pod B) ──
        synth_cfg = MODELS[self.synthesizer_model]
        print(f"\n  [synthesis] Loading {synth_cfg['filename']} for MoE final synthesis (Pod B)...")
        print(f"  [synthesis] ctx={synth_cfg['ctx']}, cache={synth_cfg.get('cache_ram',0)}MB")
        if not self.switch_model(self.synthesizer_model):
            print("  [ERROR] Synthesizer failed to load (health timeout or supervisor error)")
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
            print(f"  [ERROR] MoE synthesis failed (max_tokens={synth_cfg['max_tokens']}, "
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
        print(f"█ DevForge Multi-Agent LLM Debate v4.6 ({self.mode})")
        print(f"█ Session: {self.session_id}")
        print(f"█ Method: {self.method} | Dry-run: {self.dry_run}")
        if self.mode == "cooperative":
            print(f"█ Spot VMs → P:Qwen3-30B(:8084) R:Nemotron(:8083) | J/DRAG/Summary/Synthesis: local")
        else:
            print(f"█ Pod A (:8080): Selene-8B Judge | Pod B (:8081): Qwen-14B → DeepSeek-V2-Lite → Phi-mini-MoE")
        print(f"█ Question: {self.question[:80]}...")
        print(f"{'█'*60}")

        self._save_state({
            "type": "session_start",
            "question": self.question,
            "method": self.method,
            "skip_drag": self.skip_drag,
            "mode": self.mode,
        })

        # Stop Pod A if running — free its RAM for Pod B model switches
        if not self.dry_run:
            subprocess.run(
                ["systemctl", "--user", "stop", "container-devforge-qwen.service"],
                capture_output=True)
            print("  [pod] Pod A stopped (idle during debate)")
            time.sleep(3)  # brief cooldown for container process exit

        # Round 0: DRAG — Pod B loads model for analysis
        self.current_round = 0
        if not self.round_0_drag() and not self.skip_drag:
            print("\n[ABORT] DRAG analysis failed — cannot proceed without context")
            self._terminate_spot_vms()
            return None

        # ── Cooperative: provision Azure spot VMs ──
        spot_ok = True
        if self.mode == "cooperative":
            spot_ok = self._provision_spot_vms()
            if spot_ok:
                spot_ok = self._open_spot_tunnels()
            if not spot_ok:
                print("\n[ABORT] Spot VM provisioning failed")
                self._terminate_spot_vms()
                return None

        # Rounds 1-4: DART — Pod B switches P → R → J sequentially
        dart_ok = self.round_1_to_4_dart()

        # ── Cooperative: terminate spot VMs after debate rounds ──
        if self.mode == "cooperative":
            self._terminate_spot_vms()

        # Round 5: Synthesis — Pod B loads summary → final code
        if not dart_ok:
            print("\n[DART aborted — skipping synthesis]")
        final = self.round_5_synthesis() if dart_ok else None

        # Close all SSH tunnels (session end)
        if not self.dry_run:
            self._close_all_tunnels()

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
    ap = argparse.ArgumentParser(description="DevForge Multi-Agent LLM Debate v4.6")
    ap.add_argument("--question", "-q", required=True, help="Debate topic (File: ... Task: ...)")
    ap.add_argument("--method", "-m", default="drag",
                    choices=["drag"],
                    help="Debate method (default: drag)")
    ap.add_argument("--skip-drag", action="store_true",
                    help="Skip Round 0 DRAG context analysis")
    ap.add_argument("--dry-run", action="store_true",
                    help="Simulate without actual LLM calls")
    ap.add_argument("--mode", default="debate",
                    choices=["debate", "cooperative"],
                    help="debate (local only) or cooperative (spot VMs + local)")
    args = ap.parse_args()

    session = DebateSession(
        question=args.question,
        method=args.method,
        skip_drag=args.skip_drag,
        dry_run=args.dry_run,
        mode=args.mode,
    )
    result = session.run_session()
    if result is None:
        import sys
        sys.exit(1)


if __name__ == "__main__":
    main()

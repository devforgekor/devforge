#!/usr/bin/env python3
"""DevForge Multi-Agent LLM Debate Orchestrator v4.6

Multi-mode debate — debate (local Pod A+B) or cooperative (spot VMs + local).
No 32B — batch pipeline only.

Modes:
  debate:      Pod A (:8080) Judge + Pod B (:8081) sequential (14B+MoE local)
  cooperative: provision spot VMs from golden images -> Proposer(Qwen3-30B) + Refuter(Nemotron-3-Nano) x4 rounds
               -> terminate VMs -> Judge/DRAG/Summary/Synthesis on local Pod B

Usage:
  python3 scripts/debate.py --question "File: ...\nTask: ..." [--mode cooperative] [--skip-drag] [--dry-run]

Architecture:
  debate_data.py       — MODELS, REMOTE_HOSTS, PROMPTS (pure data)
  debate_llm.py        — call_llm, call_llm_json, switch_local_model, I/O helpers
  local_debate.py      — LocalDebate class (Pod A + Pod B)
  cooperative_remote.py — SSH tunnel management, remote_activate/deactivate
  cooperative_debate.py — CooperativeDebate(LocalDebate) with spot VM orchestration
  debate.py            — CLI dispatch (this file)
"""


def main():
    import sys as _sys
    from pathlib import Path as _Path
    _scripts_dir = str(_Path(__file__).resolve().parent)
    if _scripts_dir not in _sys.path:
        _sys.path.insert(0, _scripts_dir)

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
    ap.add_argument("--reuse-vms", action="store_true",
                    help="Reuse existing spot VMs instead of provisioning new ones")
    args = ap.parse_args()

    if args.mode == "cooperative":
        from cooperative_debate import CooperativeDebate
        session = CooperativeDebate(
            question=args.question,
            method=args.method,
            skip_drag=args.skip_drag,
            dry_run=args.dry_run,
            reuse_vms=args.reuse_vms,
        )
    else:
        from local_debate import LocalDebate
        session = LocalDebate(
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

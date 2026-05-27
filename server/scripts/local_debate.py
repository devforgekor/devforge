"""LocalDebate — multi-agent debate using local Pod A + Pod B (resident v4.6).

Pod A (:8080): DeepSeek-V2-Lite — Judge + DRAG + Summary + Synthesis (always-on)
Pod B (:8081): Qwen3-4B — Refuter (always-on)
Pod B (:8082): Phi-mini-MoE — Proposer (always-on)

All 3 models resident, no switch_file protocol. ~15.6GB RSS fits 22GB RAM.

SLOC exception (454 lines, limit 400):
  round_1_to_4_dart (~110 lines) cannot be split further without breaking cohesion.
  Proposer → Refuter → Judge form a single atomic DART cycle with shared state
  (consecutive_failures, proposer_output, refuter_output, last_disagreement).
  Extracting individual role handlers would require passing 5+ parameters through
  multiple call layers — worse readability than accepting +54 lines over the limit.
  Decision: 2026-05-27, during monolith → 6-module refactoring.
"""
import json
import random
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from debate_data import MODELS, SESSIONS_DIR
from debate_llm import (
    _build_messages,
    _extract_file_path,
    _poll_health,
    _read_file_content,
    call_llm,
    call_llm_json,
    check_early_exit,
    format_trend,
    write_report,
)


class LocalDebate:
    """Multi-agent debate orchestrator — 3 resident models, no switching."""

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
        self.mode = "debate"
        self.resident = True  # always-on models, no switch_file

        self.session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.state_dir = SESSIONS_DIR / self.session_id
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.current_round: int = 0
        self.consensus_scores: List[int] = []
        self.winner_map: List[Dict] = []
        self.drag_context: str = ""
        self._tunnels_open: set = set()

        # Resident model assignments — fixed ports, always-on
        self.drag_model = "deepseek-v2-lite"        # Pod A :8080
        self.proposer_model = "phi-mini-moe"         # Pod B :8082 (fast proposal)
        self.refuter_model = "qwen3-4b"              # Pod B :8081 (4B dense depth)
        self.judge_model = "deepseek-v2-lite"        # Pod A :8080 (coder-specialized)
        self.summary_model = "deepseek-v2-lite"      # Pod A :8080
        self.synthesizer_model = "deepseek-v2-lite"  # Pod A :8080

    # ── Persistence ────────────────────────────────────────────────────

    def _save_state(self, entry: Dict) -> None:
        entry.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        entry.setdefault("round", self.current_round)
        path = self.state_dir / "state.jsonl"
        with open(path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ── Model ready check (resident — health poll only) ────────────────

    def switch_model(self, model_id: str) -> bool:
        """Verify resident model is healthy on its fixed port. No switch_file."""
        if self.dry_run:
            port = MODELS[model_id].get("local_port", MODELS[model_id]["port"])
            print(f"  [dry-run] health check {model_id} on :{port}")
            return True
        port = MODELS[model_id].get("local_port", MODELS[model_id]["port"])
        return _poll_health(port=port, timeout=10)  # always-on, fast check

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
        return check_early_exit(self.consensus_scores)

    # ═══════════════════════════════════════════════════════════════════
    # Round handlers
    # ═══════════════════════════════════════════════════════════════════

    def round_0_drag(self) -> bool:
        """DRAG: Qwen-14B analyzes target file and sets debate context. Returns False if skipped."""
        if self.skip_drag:
            print("\n─── Round 0 (DRAG) SKIPPED (--skip-drag) ───\n")
            self._save_state({"type": "round_skip", "reason": "--skip-drag flag"})
            if not self.switch_model(self.drag_model):
                print("  [ERROR] Drag model failed to load on Pod B (skip_drag path)")
                return False
            return False

        print(f"\n{'='*60}")
        print(f"Round 0: DRAG — Context Analysis ({self.drag_model})")
        print(f"{'='*60}\n")
        self._save_state({"type": "round_start", "phase": "drag"})

        if not self.switch_model(self.drag_model):
            print("  [ERROR] Drag model failed to load on Pod B")
            return False

        file_path = _extract_file_path(self.question)
        file_content = _read_file_content(file_path) if file_path else None
        if not file_content:
            print(f"  [WARN] Could not read file: {file_path}")
            file_content = f"# File not found: {file_path}\n# Proceeding with question only."

        print(f"  [drag] Analyzing {file_path} ({len(file_content)} chars)...")

        analysis = call_llm_json(
            "drag_lite", self.drag_model, dry_run=self.dry_run,
            question=self.question,
            file_content=file_content[-12000:],
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
        print(f"  [drag] DRAG analysis complete — Judge model will be loaded for verdict + summary")
        return True

    def round_1_to_4_dart(self) -> bool:
        """DART: Rounds 1-4 — Proposer → Refuter → Judge on Pod B sequentially."""
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

            # A — Proposer
            if not self.switch_model(self.proposer_model):
                consecutive_failures += 1
                if consecutive_failures >= 2:
                    print("  [ABORT] 2 consecutive switch failures")
                    return False
                continue
            proposer_output = call_llm_json(
                "dart_proposer", self.proposer_model, dry_run=self.dry_run,
                question=self.question,
                drag_context=drag_ctx,
                history_summary=history_summary,
                consensus_score=str(self.consensus_scores[-1] if self.consensus_scores else "N/A"),
                disagreement_points=last_disagreement,
                refuter_last_output=json.dumps(refuter_output, indent=2) if refuter_output else "N/A (first round)",
            )
            if not proposer_output:
                consecutive_failures += 1
                if consecutive_failures >= 2:
                    print("  [ABORT] 2 consecutive round failures")
                    return False
                continue
            self._save_state({"type": "llm_response", "phase": "dart_proposer",
                              "model": self.proposer_model, "output": proposer_output})

            # B — Refuter
            if not self.switch_model(self.refuter_model):
                consecutive_failures += 1
                if consecutive_failures >= 2:
                    print("  [ABORT] 2 consecutive switch failures")
                    return False
                continue
            refuter_output = call_llm_json(
                "dart_refuter", self.refuter_model, dry_run=self.dry_run,
                question=self.question,
                drag_context=drag_ctx,
                history_summary=history_summary,
                consensus_score=str(self.consensus_scores[-1] if self.consensus_scores else "N/A"),
                disagreement_points=last_disagreement,
                proposer_last_output=json.dumps(proposer_output, indent=2),
            )
            if not refuter_output:
                consecutive_failures += 1
                if consecutive_failures >= 2:
                    print("  [ABORT] 2 consecutive round failures")
                    return False
                continue
            self._save_state({"type": "llm_response", "phase": "dart_refuter",
                              "model": self.refuter_model, "output": refuter_output})

            # C — Judge
            if not self.switch_model(self.judge_model):
                print("  [ERROR] judge model switch failed")
                continue
            alpha, beta, _, _ = self._shuffle_proposals(proposer_output, refuter_output)
            judge_output = call_llm_json(
                "dart_judge", self.judge_model, dry_run=self.dry_run,
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

            consecutive_failures = 0
            self._save_state({
                "type": "judge_verdict",
                "consensus_score": score,
                "winner_label": judge_output.get("winner"),
                "winner_model": winner,
                "output": judge_output,
            })

            print(f"  Consensus: {score}% | Winner: {winner} | "
                  f"Trend: {format_trend(self.consensus_scores)}")

        return True

    def round_5_synthesis(self) -> Optional[dict]:
        """Synthesis: summary + final code on Pod B."""
        print(f"\n{'='*60}")
        print(f"Round 5: Synthesis ({self.summary_model} summary + {self.synthesizer_model} synthesis)")
        print(f"{'='*60}\n")
        self.current_round = 5
        self._save_state({"type": "round_start", "phase": "synthesis"})

        state_path = self.state_dir / "state.jsonl"
        full_history = state_path.read_text() if state_path.exists() else ""
        consensus_trend = format_trend(self.consensus_scores)

        # Step 1: Summary
        print(f"  [summary] {self.summary_model} writing debate summary...")
        if not self.switch_model(self.summary_model):
            print("  [ERROR] Summary model switch failed")
            return None
        summary_raw = call_llm(
            _build_messages("history_summary", self.summary_model,
                            question=self.question,
                            full_history=full_history[-8000:],
                            consensus_trend=consensus_trend),
            self.summary_model,
            dry_run=self.dry_run,
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

        # Post-summary hook (CooperativeDebate closes Judge/Gemma tunnel here)
        self._post_summary_hook()

        # Step 2: Final synthesis (DeepSeek-V2-Lite, already resident on :8081)
        if not self.switch_model(self.synthesizer_model):
            print("  [ERROR] Synthesizer health check failed")
            return None

        drag_ctx = self.drag_context or json.dumps({"note": "no DRAG context"})
        final = call_llm_json(
            "final_synthesis", self.synthesizer_model, dry_run=self.dry_run,
            question=self.question,
            history_summary=history_summary,
            consensus_trend=consensus_trend,
            drag_context=drag_ctx,
        )
        if not final:
            cfg = MODELS.get(self.synthesizer_model, {})
            print(f"  [ERROR] synthesis failed (max_tokens={cfg.get('max_tokens','?')}, "
                  f"bench_toks={cfg.get('bench_toks','?')})")
            return None

        self._save_state({"type": "final_synthesis",
                          "model": self.synthesizer_model,
                          "output": final})

        print(f"\n  [done] Final synthesis complete: confidence={final.get('confidence', 'N/A')}")
        return final

    # ── Extension hooks (overridden by CooperativeDebate) ──────────────

    def _pre_dart_hook(self) -> bool:
        """Called after DRAG, before DART rounds. Return False to abort."""
        return True

    def _post_dart_hook(self) -> None:
        """Called after DART rounds, before synthesis."""

    def _post_summary_hook(self) -> None:
        """Called after summary, before final synthesis."""

    def _cleanup_hook(self) -> None:
        """Called after synthesis for cleanup (tunnels, spot VMs)."""

    def _enqueue_for_review(self, final: dict) -> None:
        """Enqueue debate result to activity_log for night batch review (14B→32B)."""
        try:
            from lib.queue_writer import enqueue_review
            enqueue_review(
                entry_type="debate_result",
                source="local_debate",
                title=f"debate: {self.question[:80]}",
                summary=f"consensus={self.consensus_scores[-1] if self.consensus_scores else '?'}%, "
                        f"confidence={final.get('confidence', '?')}, "
                        f"rounds={len(self.consensus_scores)}",
                body={
                    "session_id": self.session_id,
                    "question": self.question[:200],
                    "method": self.method,
                    "mode": self.mode,
                    "consensus_scores": self.consensus_scores,
                    "consensus_trend": format_trend(self.consensus_scores),
                    "final_diff": final.get("diff", "")[:5000],
                    "decision_summary": final.get("decision_summary", "")[:1000],
                    "security_notes": final.get("security_notes", []),
                    "performance_notes": final.get("performance_notes", []),
                    "confidence": final.get("confidence"),
                },
                model=self.synthesizer_model,
                tags=["debate", self.mode],
            )
        except Exception as e:
            print(f"  [queue] Failed to enqueue debate result: {e}")

    # ═══════════════════════════════════════════════════════════════════
    # Main loop
    # ═══════════════════════════════════════════════════════════════════

    def _print_header(self) -> None:
        print(f"\n{'█'*60}")
        print(f"█ DevForge Multi-Agent LLM Debate v4.6 ({self.mode}, resident)")
        print(f"█ Session: {self.session_id}")
        print(f"█ Method: {self.method} | Dry-run: {self.dry_run}")
        print(f"█ Pod A (:8080): DeepSeek-V2-Lite Judge+DRAG+Synthesis")
        print(f"█ Pod B (:8081): Qwen3-4B Refuter | :8082: Phi-mini-MoE Proposer")
        print(f"█ Question: {self.question[:80]}...")
        print(f"{'█'*60}")

    def run_session(self) -> Optional[dict]:
        self._print_header()

        self._save_state({
            "type": "session_start",
            "question": self.question,
            "method": self.method,
            "skip_drag": self.skip_drag,
            "mode": self.mode,
        })

        # Ensure Pod A is running (DeepSeek-V2-Lite Judge/DRAG/Summary/Synthesis)
        if not self.dry_run:
            subprocess.run(
                ["systemctl", "--user", "start", "container-devforge-qwen.service"],
                capture_output=True)
            print("  [pod] Pod A start requested (DeepSeek-V2-Lite :8080)")
            # Brief wait for container init, then health check
            time.sleep(5)
            if not _poll_health(port=8080, timeout=30):
                print("  [WARN] Pod A :8080 health check failed — continuing anyway")

        # Round 0: DRAG
        self.current_round = 0
        if not self.round_0_drag() and not self.skip_drag:
            print("\n[ABORT] DRAG analysis failed — cannot proceed without context")
            return None

        # Pre-DART hook (spot VM provisioning in cooperative mode)
        if not self._pre_dart_hook():
            print("\n[ABORT] Pre-DART hook failed")
            return None

        # Rounds 1-4: DART
        dart_ok = self.round_1_to_4_dart()

        # Post-DART hook (spot VM termination in cooperative mode)
        self._post_dart_hook()

        # Round 5: Synthesis
        if not dart_ok:
            print("\n[DART aborted — skipping synthesis]")
        final = self.round_5_synthesis() if dart_ok else None

        # Cleanup hook (close tunnels)
        self._cleanup_hook()

        # Write report + upload
        if final:
            report_path = write_report(self.state_dir, self.session_id,
                                        self.question, self.method,
                                        self.consensus_scores, final)
            print(f"\n{'█'*60}")
            print(f"█ DEBATE COMPLETE")
            print(f"█ Session: {self.session_id}")
            print(f"█ Confidence: {final.get('confidence', '?')}")
            print(f"█ Local: {report_path}")

            # Enqueue for night batch review (14B → 32B)
            self._enqueue_for_review(final)

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
                        "consensus_trend": format_trend(self.consensus_scores),
                        "confidence": str(final.get("confidence", "?")),
                    },
                )
                print(f"█ Review: {url}")
            except Exception as e:
                print(f"█ Upload skipped: {e}")

            print(f"{'█'*60}")

        return final

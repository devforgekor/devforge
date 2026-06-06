# DevForge — Governed Vibe Coding: Master Refactoring Plan

**Version**: 1.0
**Date**: 2026-06-03
**Status**: ACTIVE
**Language**: English (machine-readable). User-facing text remains Korean per llm-common-rule.md.
**Reference**: `docs/domain-glossary.yaml`, `_archive/agent-architecture.yaml` (v3.0.1), `docs/plans/master-plan.md` (v2.0)

---

## 1. Problem Statement

DevForge currently operates without a shared rule framework across its three AI tiers:

| Tier | Agent | Context | Flaw |
|:-----|:------|:--------|:-----|
| Cloud | Claude Code (Opus 4.8) | 200K | Rules exist but are monolithic (common-rule.md); no DDD/TDD workflow guidance |
| Cloud | Aider (DeepSeek v4-flash) | ~128K | No rule file at all — no `CONVENTIONS.md` |
| Local | llama.cpp (Qwen/Selene/R1, 4K-16K) | 4K-16K | System prompts hardcoded across 5+ Python files; no shared guardrails; no glossary injection |

Consequences:
- DDD/TDD principles remain aspirational — agents don't receive them as system instructions
- Adding a rule (e.g. "never hallucinate imports") requires editing 5+ Python files
- LLM-generated diffs pass through no deterministic validation gate before commit
- No unified glossary — each script invents its own terminology for the same domain concepts

---

## 2. Target Architecture

```
                       ┌──────────────────────────┐
                       │   Rule & Glossary Layer   │
                       │  agent-rule / local-rule  │
                       │  glossary (70+ terms)     │
                       └────────────┬─────────────┘
                                    │ injected at call time
             ┌──────────────────────┼──────────────────────┐
             ▼                      ▼                      ▼
     ┌──────────────┐      ┌──────────────┐      ┌──────────────┐
     │  Cloud AI    │      │  Production   │      │  External API│
     │ Claude Code  │      │  Line (Local) │      │  DeepSeek    │
     │ Aider        │      │  Qwen/Selene │      │  Gemini      │
     └──────┬───────┘      └──────┬────────┘      └──────┬───────┘
            │                     │                      │
            └─────────────────────┼──────────────────────┘
                                  │
                     ┌────────────┼────────────┐
                     ▼            ▼            ▼
             ┌──────────┐ ┌──────────┐ ┌──────────────┐
             │  Draft   │ │Cooperative│ │   30B        │
             │Claude+|User│ │ Debate   │ │  Finalize    │
             └────┬─────┘ │ (Azure VM)│ └──────┬───────┘
                  │       └─────┬────┘        │
                  │             │             │
                  └─────────────┼─────────────┘
                                ▼
                       ┌──────────────────┐
                       │  Nightly Verify  │
                       │  Ph4(3-model)→Ph5│
                       │  (27B)→Ph7(API)  │
                       └───────┬──────────┘
                               │
                     ┌─────────┴─────────┐
                     ▼                   ▼
             ┌──────────────┐  ┌──────────────────┐
             │ Activity DB  │  │ Observer (HUD)    │
             │ + Worklog    │  │ shows flow status │
             └──────────────┘  └──────────────────┘
```

---

## 3. Four Phase Roadmap

```
Phase 1 (✅ done)        Phase 2 (next)         Phase 3 (2-4 weeks)
Rule File Separation     Prompt Centralization  Observer HUD
───────────────────────────────────────────────────────────────────
agent-rule.md            prompt_builder.py      observer.py (HUD)
local-rule.md            hardcoded prompts →    real-time flow viz
glossary.yaml ✓          factory pattern        confidence tracking
                                                queue monitoring
```

### Priority: MCP Mode Preparation + Criteria-First

The ENTIRE production line exists to prepare for MCP mode. Every phase, every script, every model serves this goal. And every step follows the rule: **criteria first, implementation second**.

- MCP mode prep is the primary objective — all other work supports it.
- Before implementing ANY component: define pass/fail criteria via WebSearch → draft → user finalization.
- Cooperative debate (Azure VMs) = file production. Night mode (27B verify + Phase 4/5/7) = validation. DeepSeek API = optional efficiency multiplier, not a required gate.

### Phase 1 — Rule File Separation (Today)

**Goal**: Establish the rule base layer. Every AI tier gets its own rule file with appropriate detail level.

**Context from codebase analysis** (2026-06-03):
- Read `PLAN.md` (v2.0, 2026-05-18): 4-tier model strategy (Fast→Accurate→External), v1.1→B-Plan worklog reconciliation, timeline through Jun 14
- Read `agent-architecture.yaml` (v3.0.1, 2026-05-29): Day/Night Bundle architecture, 3-model review pipeline (R1→Qwen7B→Selene), Supervisor-Worker-Observer proposal (Section 6), memory budgets, context budgets, swap sequences
- Read `server-specs-and-llm-architecture.md`: current mode map (code/debate/batch/normal/discussion), 4-stage pipeline, debate architecture, port assignments
- Generated `docs/glossary.md`: 10 Bounded Contexts, 70+ domain terms from codebase reverse-engineering

**Files to create**:

| File | Location | Lines | Audience |
|:-----|:---------|:------|:---------|
| `agent-rule.md` | `/home/opc/` | ~30 | Cloud agents (Claude Code, Aider) |
| `local-rule.md` | `/opt/projects/server/docs/` | ~80 | Local LLMs (Qwen/Selene/R1/Phi-4) |

**Files to modify**:

| File | Change |
|:-----|:--------|
| `/home/opc/CLAUDE.md` | Add `@./agent-rule.md` include |

**Design decisions**:

- `agent-rule.md` — principles only. Cloud agents have 128K-200K context; they execute well from high-level directives. Content: session startup checklist, TDD/DDD principles, language pipeline, Python standards summary, session end checklist.
- `local-rule.md` — strict guardrails. Local LLMs have 4K-16K context and hallucinate more. Content: output constraints (JSON only, no placeholders, complete code), anti-hallucination rules (no guessed imports, say "I don't know"), context isolation (tagged files only), TDD protocol (Red→stop→Green→Refactor only if asked), role-specific cages (Reviewer/Reflector/Judge/DiffWriter/CodeGen), error recovery (invalid JSON→re-output, 2 failures→ask human).
- `local-rule.md` is designed as a **shared preamble** — in Phase 2, `lib/llm_client.py` will automatically prepend it to role-specific system prompts. No Python code changes in Phase 1.

**File relationship after Phase 1**:

```
CLAUDE.md
├── @./infrastructure.md   (server identity — auto-gen from CLAUDE.yaml)
├── @./llm-common-rule.md   (LLM common rules — all agents)
└── @./llm-agent-rule.md    (cloud agent principles — Claude Code, Aider)

llm-local-rule.md           (local LLM guardrails — standalone)
  → Phase 2: auto-prepended by lib/llm_client.py
```

### Phase 2 — Prompt Centralization (Next Session)

**Goal**: Single source of truth for all AI prompts. Adding a rule = editing one file, not five.

**Context from codebase analysis**:
- System prompts are currently hardcoded in 5+ files: `review_pipeline_steps.py` (`SYSTEM_REVIEW_STEP1/2/3`), `review_consumer.py` (`VERIFY_SYSTEM`, `ANALYSIS_SYSTEM`), `proxy_reviewer.py` (`DEEPSEEK_SYSTEM_SINGLE/BATCH`), `code_mod_pipeline.py` (inline prompts), `debate_llm.py` (`PROMPTS` dict)
- Two separate LLM client modules exist: `lib/llm_client.py` (local llama.cpp, `call_llm()`, `call_llm_json()`, `MODEL_REGISTRY`, feedback auto-inject) and `lib/llm/client.py` (DeepSeek API + local, `call_llm(endpoint)`, system→user conversion, keepalive)
- These two clients have different call signatures, error handling, and model registries

**Implementation**:

```
New file: lib/prompt_builder.py (~80 lines)
  build_prompt(role, domain) → local-rule + glossary[domain] + role-specific prompt

  Roles: "reviewer" | "reflector" | "judge" | "diff_writer" | "verifier" | "auditor" | "codegen"
  Domains: "review" | "code_mod" | "debate" | "general"

Scripts updated (remove hardcoded prompts):
  review_pipeline_steps.py   → build_prompt("reviewer", "review")
  review_consumer.py         → build_prompt("verifier", "review")
  proxy_reviewer.py          → build_prompt("auditor", "review")
  code_mod_pipeline.py       → build_prompt("codegen", "code_mod")
  debate_llm.py              → build_prompt("debate", "general")

lib/llm_client.py updated:
  call_llm() auto-prepends local-rule.md to system messages

Aider integration:
  ln -sf CLAUDE.md CONVENTIONS.md  (project root)
```

### Phase 3 — Observer HUD (2-4 Weeks)

**Goal**: Real-time visibility into the production line. What's running, what's queued, where's the bottleneck.

**Role**: Observer is a **HUD (heads-up display)**, not a validation gate. It watches:
- Day mode: extract/MCP tasks, cooperative debate sessions, 30B file finalization
- Night mode: Phase 4 review → Phase 5 verify → Phase 7 audit, queue depth, pass/fail rates
- Confidence regression: are local LLM outputs trending toward ≥90% or away from it?

**Implementation**:

```
Existing: observer.py (~300 lines) — already exists in scripts/
  → Repurpose as HUD:
    - Query activity_log for pipeline state
    - Summarize: what's running, what's queued, what failed
    - Track confidence markers (# LOW-CONFIDENCE, # UNVERIFIED) across outputs
    - No deterministic validation. No agent_loop state machine. No fix strategies.

Output: structured status (JSON → MOTD + CLI):
  {
    "day": {"active": "cooperative_debate", "queue": 3},
    "night": {"phase": "5_verify", "items": 12, "passed": 10, "failed": 2},
    "confidence_trend": {"min": 72, "avg": 88, "trend": "up"}
  }

Integration:
  - MOTD shows summary on SSH login
  - cli.py status subcommand (basic) → observer.py (detailed)
  - Optional: webhook to Telegram on bottleneck detection
```

### Phase 4 — (Removed)

TDD CLI subcommand is unnecessary complexity. The production line already produces files through cooperative debate and validates through night mode. Adding a TDD-specific CLI adds surface area without proportional benefit.

---

## 4. Design Principles (All Phases)

1. **Rules above code** — Rules are injected as system instructions before any AI generates code
2. **Verification above generation** — Night mode (Phase 4→5→7) validates before outputs reach production. Local LLM self-verifies (pytest, import-check, confidence assessment) before submission.
3. **Single source of truth** — System prompts, glossary terms, model registry each live in exactly one file
4. **Incremental adoption** — One phase at a time, validated before proceeding to the next
5. **Role cages for local LLMs** — Each model role has explicit output boundaries (Reviewer finds bugs only, never writes code; DiffWriter outputs unified diff only, never suggests; Judge votes only, never creates findings)

---

## 5. Why This Order

| Order | If skipped |
|:------|:-----------|
| Phase 1 first | Rules don't exist for any tier. AI repeats mistakes across sessions. |
| Phase 2 next | Changing a rule requires editing 5+ files. Human error + omission inevitable. |
| Phase 3 last | No visibility into production line. Bottlenecks invisible until they break the nightly window. |

---

## 6. Risk Assessment

| Risk | Phase | Mitigation |
|:-----|:------|:-----------|
| local-rule.md too long for 4K context windows | 1-2 | Glossary terms injected per-domain (not all 70+ at once); rule preamble kept under 200 tokens |
| prompt_builder.py breaks existing pipeline | 2 | Each script migrated one at a time; git diff before/after on sample inputs |
| Observer HUD adds noise without insight | 3 | Start with 3 metrics (active tasks, night results, confidence trend). Expand based on debugging needs. |
| Nightly pipeline time creep | — | 27B + 32B sequential verify has bounded wall time. Phase 7 (DeepSeek) is optional per item. |

---

## 7. Success Metrics

| Metric | Baseline | Target | Measured By |
|:-------|:---------|:-------|:------------|
| System prompt change propagation | Edit 5+ files | Edit 1 file (`prompt_builder.py`) | File count touched per rule change |
| Local LLM output confidence | No tracking | min >= 90% across all outputs | `# CONFIDENCE:` markers parsed from outputs |
| Night mode throughput | Manual monitoring | Observer HUD shows real-time queue depth + phase status | observer.py -> MOTD + CLI |
| Production line visibility | 0 (blind) | Observer shows day/night state, active tasks, bottleneck alerts | CLI `status` subcommand |
| Glossary term consistency | Each script invents own names | All scripts use mapped terms | `grep domain-glossary.yaml` in `scripts/` |

---

## 8. References

- `docs/domain-glossary.yaml` — DDD Ubiquitous Language (10 Bounded Contexts, 70+ terms)
- `_archive/agent-architecture.yaml` v3.0.1 — Complete system specification, model catalog, review pipeline, Supervisor-Worker-Observer proposal (archived)
- `docs/plans/master-plan.md` v2.0 — Previous unified plan (2026-05-18), model strategy, timeline
- `_archive/server-specs-and-llm-architecture.md` — Mode map, 4-stage pipeline, debate architecture, port assignments (archived)
- `docs/specs/system-design.yaml` — Core architecture decisions, infrastructure layout
- `docs/phases.md` — Phase tracker (Phase 1 complete, 1.5 complete, 2 active)
- `handover.yaml` — Session state, current decisions, known issues

---

*Generated: 2026-06-03 | Machine-readable | Updates via PR to docs/plans/refactoring-roadmap.md*

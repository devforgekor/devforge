#!/usr/bin/env python3
"""gen_motd_task.py — Generate DevForge MOTD+Task summary cache file.

Default: writes /tmp/devforge-motd-task.txt (called by motd-gen.service timer).
With --stdout: prints to stdout.

Data sources: state.yaml, tasks.yaml, handover.yaml, nightly_status.yaml,
PostgreSQL (turns, worklog, decisions).
"""

import subprocess
import sys
from datetime import date, timezone
from pathlib import Path

import yaml

SERVER = Path("/opt/projects/server")
STATE_FILE = SERVER / "state.yaml"
TASKS_FILE = SERVER / "docs/tasks.yaml"
HANDOVER_FILE = SERVER / "handover.yaml"
NIGHTLY_FILE = SERVER / "docs/nightly_status.yaml"
CLI = str(SERVER / "scripts/cli.py")
CACHE_FILE = Path("/tmp/devforge-motd-task.txt")

# ANSI
GREEN  = "\033[0;32m"
RED    = "\033[0;31m"
YELLOW = "\033[1;33m"
ORANGE = "\033[0;33m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
NC     = "\033[0m"

AGENT_ORDER = ("claude-code", "copilot", "gemini", "qwen")
AGENT_DISPLAY = {
    "claude-code": "Claude",
    "copilot": "Copilot",
    "gemini": "Gemini",
    "qwen": "Qwen",
}
COL_WIDTH = 7  # visible width for each value column


def load_yaml(path: Path):
    try:
        with open(path) as f:
            return yaml.safe_load(f)
    except Exception:
        return {}


def psql(query: str, timeout=5):
    try:
        r = subprocess.run(
            ["podman", "exec", "postgres", "psql", "-U", "devforge",
             "-d", "devforge_app", "--no-align", "--tuples-only", "-c", query],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return ""


def yesterday_turns_decisions():
    """Return {agent: {turns, decisions}} for yesterday from DB."""
    out = psql(
        "SELECT t.agent, COUNT(*) AS turns, COUNT(d.turn_id) AS decisions"
        " FROM turns t LEFT JOIN obs_dec d ON d.turn_id = t.id"
        " WHERE t.created_at >= CURRENT_DATE - 1"
        " AND t.created_at < CURRENT_DATE"
        " GROUP BY t.agent ORDER BY 2 DESC"
    )
    result = {}
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) >= 3:
            agent = parts[0].strip()
            try:
                result[agent] = {
                    "turns": int(parts[1].strip()),
                    "decisions": int(parts[2].strip()),
                }
            except ValueError:
                pass
    return result


def yesterday_observations():
    """Return total observation count from yesterday."""
    out = psql(
        "SELECT COUNT(*) FROM observations "
        "WHERE created_at >= CURRENT_DATE - 1 "
        "AND created_at < CURRENT_DATE"
    )
    try:
        return int(out.strip())
    except (ValueError, TypeError):
        return 0


def parse_agent_from_title(title):
    """Infer agent key from task title prefix or content."""
    t = title.strip()
    tl = t.lower()
    # Compound patterns (checked before simple prefixes)
    if "qwen" in tl:
        return "claude-code"  # Qwen worker audits done by claude-code
    # Direct prefixes
    if tl.startswith("copilot"):
        return "copilot"
    if tl.startswith("gemini"):
        return "gemini"
    if tl.startswith("claude"):
        return "claude-code"
    # Keyword-based attribution
    if "workspace" in tl or "reboot" in tl:
        return "claude-code"
    if "motd" in tl or "token" in tl:
        return "copilot"
    if "deepseek" in tl:
        return "copilot"
    if "decision" in tl or "migration" in tl:
        return "copilot"
    if "terminology" in tl:
        return "copilot"
    return None


def task_counts_from_yaml():
    """Count per-agent completed tasks from tasks.yaml done list.
    Returns {agent: count} and total_tasks across all lists."""
    tasks = load_yaml(TASKS_FILE)
    done = tasks.get("done", [])
    todo = tasks.get("todo", [])
    blocked = tasks.get("blocked", [])
    in_progress = tasks.get("in_progress")

    counts = {a: 0 for a in AGENT_ORDER}
    unknown = 0

    for item in done:
        title = item if isinstance(item, str) else item.get("title", "")
        agent = parse_agent_from_title(title)
        if agent in counts:
            counts[agent] += 1
        else:
            unknown += 1

    total = len(done) + len(todo) + len(blocked) + (1 if in_progress else 0)
    return counts, total, unknown


def nightly_color(nightly):
    """Determine color based on nightly status.
    Returns (tasks_color, decisions_color).
    - Normal: NC
    - Not run yet (pending): YELLOW
    - link broken: RED
    - embed broken: ORANGE (decisions unknown)
    """
    if not nightly:
        return YELLOW, YELLOW

    link_ok = nightly.get("link_turns") == "ok"
    emb_ok = nightly.get("embed_turns") == "ok"

    if not link_ok and not emb_ok:
        return RED, RED
    if not link_ok:
        return RED, NC
    if not emb_ok:
        return NC, ORANGE
    return NC, NC


def recent_worklog(limit=5):
    """Return title lines only: [DATE] title  (agent/model)"""
    try:
        r = subprocess.run(
            [sys.executable, CLI, "worklog", "recent", "--limit", str(limit)],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return [l for l in r.stdout.strip().splitlines() if l.startswith("[20")]
    except Exception:
        pass
    return []


def color_pct(pct, warn=80, crit=90):
    if pct >= crit:
        return f"{RED}{pct}%{NC}"
    elif pct >= warn:
        return f"{YELLOW}{pct}%{NC}"
    return f"{pct}%"


def color_cpu(val):
    if val >= 3.5:
        return f"{RED}{val:.1f}{NC}"
    elif val >= 2.5:
        return f"{YELLOW}{val:.1f}{NC}"
    return f"{val:.1f}"


def pad_val(val_str, width=COL_WIDTH):
    """Pad visible text to fixed width, then wrap with color."""
    padded = f"{val_str:<{width}}"
    return padded


def format_task_row(label, counts, total, color):
    """Format Tasks row: done/total from tasks.yaml."""
    row = f"  {label:<9}"
    for agent in AGENT_ORDER:
        done = counts.get(agent, 0)
        raw = f"{done}/{total}"
        row += f"  {AGENT_DISPLAY[agent]:<7} {color}{pad_val(raw)}{NC}"
    return row.rstrip()


def format_decision_row(label, db_stats, color):
    """Format Decisions row: decisions/turns from DB."""
    row = f"  {label:<9}"
    for agent in AGENT_ORDER:
        s = db_stats.get(agent, {"turns": 0, "decisions": 0})
        raw = f"{s['decisions']}/{s['turns']}"
        row += f"  {AGENT_DISPLAY[agent]:<7} {color}{pad_val(raw)}{NC}"
    return row.rstrip()


def build_motd_task(include_legend=False):
    state = load_yaml(STATE_FILE)
    structural = state.get("structural", {})
    metrics    = state.get("metrics", {})

    containers = structural.get("containers", [])
    app_ctr = [c for c in containers if not c["name"].startswith("pod:")]
    total_ctr = len(app_ctr)
    healthy_ctr = sum(1 for c in app_ctr if "healthy" in c.get("status", "").lower())
    unhealthy_ctr = sum(1 for c in app_ctr if "unhealthy" in c.get("status", "").lower())

    services = structural.get("services", [])
    active_svc = sum(1 for s in services if s.get("status") == "active")
    failed_svc = [s["name"] for s in services if s.get("status") not in ("active", "inactive")]

    sys_m = metrics.get("system", {})
    cpu_load = sys_m.get("cpu_load", [0, 0, 0])
    cpu_cores = sys_m.get("cpu_cores", 4)
    mem = sys_m.get("memory", {})
    mem_used = mem.get("used", "?")
    mem_total = mem.get("total", "?")
    mem_pct = mem.get("percent", 0)

    storage = metrics.get("storage_use", [])
    alerts = metrics.get("alerts", [])
    validation = metrics.get("validation")

    tasks = load_yaml(TASKS_FILE)
    in_progress = tasks.get("in_progress")
    handover = load_yaml(HANDOVER_FILE)
    known_issues = handover.get("known_issues", [])
    nightly = load_yaml(NIGHTLY_FILE)

    lines = []

    # ── Line 1: health bar ──────────────────────────────────────────
    parts = ["DevForge"]

    if unhealthy_ctr > 0:
        parts.append(f"컨테이너 {GREEN}{healthy_ctr}{NC}/{total_ctr} {RED}{unhealthy_ctr}↓{NC}")
    else:
        parts.append(f"컨테이너 {GREEN}{healthy_ctr}/{total_ctr}{NC}")

    svc_text = f"서비스 {GREEN}{active_svc}{NC}"
    if failed_svc:
        svc_text += f" {RED}{len(failed_svc)}↓{NC}"
    parts.append(svc_text)

    cpu_parts = [color_cpu(v) for v in cpu_load[:3]] + [str(cpu_cores)]
    parts.append(f"CPU {'/'.join(cpu_parts)}")

    mp = color_pct(mem_pct, 80, 90)
    zram = sys_m.get("zram", "")
    zram_cycles = sys_m.get("zram_cycles", 0)
    if zram:
        parts.append(f"Mem {mem_used}/{mem_total} ({mp}) | Zram {zram} (×{zram_cycles})")
    else:
        parts.append(f"Mem {mem_used}/{mem_total} ({mp})")

    obs_count = yesterday_observations()
    if obs_count > 0:
        parts.append(f"Obs {obs_count}")

    lines.append(" | ".join(parts))

    # ── Nightly color ───────────────────────────────────────────────
    tc, dc = nightly_color(nightly)

    # ── Line 2+3: agent stats ───────────────────────────────────────
    task_counts, total_tasks, _ = task_counts_from_yaml()
    db_stats = yesterday_turns_decisions()

    if total_tasks > 0:
        lines.append(format_task_row("Tasks", task_counts, total_tasks, tc))
        lines.append(format_decision_row("Decisions", db_stats, dc))

    # ── Status summary ──────────────────────────────────────────────
    status_bits = []

    if in_progress:
        status_bits.append(f"진행: {ORANGE}{BOLD}{in_progress}{NC}")

    if known_issues:
        status_bits.append(f"{YELLOW}이슈 {len(known_issues)}건{NC}")

    if status_bits:
        lines.append(" · ".join(status_bits))

    # ── Known issues detail ─────────────────────────────────────────
    if known_issues:
        lines.append(f"\n{BOLD}[알려진 이슈]{NC}")
        for i in known_issues[:2]:
            lines.append(f"  {YELLOW}•{NC} {i[:100]}")

    # ── Warnings ────────────────────────────────────────────────────
    warnings = []

    if len(cpu_load) >= 3 and cpu_load[2] >= 2.5:
        warnings.append(f"CPU 부하: 15분 평균 {color_cpu(cpu_load[2])} (1m:{cpu_load[0]:.1f} 5m:{cpu_load[1]:.1f} 15m:{cpu_load[2]:.1f})")

    if mem_pct >= 80:
        warnings.append(f"메모리 부족: {mem_used}/{mem_total} ({color_pct(mem_pct, 80, 90)})")

    for s in storage:
        pct = s.get("use_pct", 0)
        if pct >= 80:
            label = "심각" if pct >= 95 else "경고"
            warnings.append(f"저장소 {label}: {s['device']} ({s['mount']}) {color_pct(pct, 80, 95)}")

    if unhealthy_ctr:
        names = [c["name"] for c in app_ctr if "unhealthy" in c.get("status", "").lower()]
        warnings.append(f"{RED}컨테이너 장애:{NC} {', '.join(names)}")

    if failed_svc:
        warnings.append(f"{RED}서비스 장애:{NC} {', '.join(failed_svc)}")

    if validation and validation.get("status") == "fail":
        n = len(validation.get("mismatches", []))
        warnings.append(f"{RED}설정 불일치:{NC} {n}개 항목")

    for a in alerts[:5]:
        warnings.append(f"{RED}⚠{NC} {a}")

    if warnings:
        lines.append(f"\n{BOLD}[주의]{NC}")
        for w in warnings:
            lines.append(f"  {w}")

    # ── Recent worklog ──────────────────────────────────────────────
    entries = recent_worklog(5)
    if entries:
        lines.append(f"\n{BOLD}[최근 작업]{NC}")
        for line in entries[:5]:
            lines.append(f"  {DIM}{line}{NC}")

    # ── Color legend ────────────────────────────────────────────────
    if include_legend:
        lines.append(f"\n{DIM}[색상]{NC} {YELLOW}노랑{NC}=nightly 미실행  {ORANGE}주황{NC}=embed깨짐/진행중  {RED}빨강{NC}=링크깨짐")

    return "\n".join(lines) + "\n"


def main():
    # Include legend only if explicitly requested via --with-legend
    include_legend = "--with-legend" in sys.argv
    motd_task = build_motd_task(include_legend=include_legend)

    if "--stdout" in sys.argv:
        print(motd_task, end="")
    else:
        try:
            CACHE_FILE.write_text(motd_task)
        except Exception:
            pass


if __name__ == "__main__":
    main()

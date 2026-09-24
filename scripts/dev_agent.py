#!/usr/bin/env python3
# Status: experimental
# Path: manual first; systemd/user/devforge-dev-agent.service (later)
"""Agent Issue->PR runner.

Implement one claimed auto-safe issue on an isolated git worktree, gate on
ruff+pytest, push the branch, and open a PR (review only — never merges).

[WARNING] The agent edits production code. Isolation: a throwaway worktree
(main checkout untouched), no Key Vault secrets injected, hard timeout, and
auto-safe issues only. Default is --dry-run; pass --execute to actually run.
"""
import argparse
import os
import subprocess
import sys
from typing import Optional

REPO = "/opt/projects/server"
WORKTREE_ROOT = "/tmp/dev-agent"
sys.path.insert(0, os.path.join(REPO, "scripts"))

from lib.dev_pipeline import DEFAULT_REPO, _gh_env, _load_state, create_pr  # noqa: E402


def _run(cmd: list[str], cwd: str, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def _gh(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh"] + args, capture_output=True, text=True, timeout=timeout,
        env=_gh_env(),
    )


def resolve_issue(explicit: Optional[int]) -> Optional[int]:
    """Explicit issue number, else the oldest claimed issue without a PR."""
    if explicit:
        return explicit
    state = _load_state()
    claimed = state.get("claimed", {})
    pr_created = state.get("pr_created", {})
    pending = sorted((int(k) for k in claimed if k not in pr_created))
    return pending[0] if pending else None


def fetch_issue(number: int) -> Optional[dict]:
    result = _gh(
        ["issue", "view", str(number), "--repo", DEFAULT_REPO,
         "--json", "number,title,body,labels,state"]
    )
    if result.returncode != 0:
        return None
    import json

    return json.loads(result.stdout)


def is_auto_safe(issue: dict) -> bool:
    return any(label.get("name") == "auto-safe" for label in issue.get("labels", []))


def build_prompt(issue: dict) -> str:
    return (
        f"You are implementing GitHub issue #{issue['number']} in this repository.\n"
        f"Title: {issue['title']}\n\n{issue.get('body', '')}\n\n"
        "Work autonomously: make the minimal correct change, add/adjust tests, and "
        "run `uv run --frozen ruff check src/ tests/` and "
        "`uv run --frozen pytest tests/unit tests/fitness -x --tb=short` until green. "
        "Do NOT commit, push, or open a PR — the runner does that after gating. "
        "Do NOT touch secrets or files outside the repo."
    )


def run_agent(worktree: str, prompt: str, model: str, timeout: int) -> int:
    cmd = ["opencode", "run", prompt, "--dir", worktree]
    if model:
        cmd += ["--model", model]
    result = _run(cmd, cwd=worktree, timeout=timeout)
    sys.stdout.write(result.stdout[-4000:])
    sys.stderr.write(result.stderr[-2000:])
    return result.returncode


def gate(worktree: str) -> bool:
    """Return True only when lint + tests pass in the worktree."""
    for cmd in (
        ["uv", "run", "--frozen", "ruff", "check", "src/", "tests/"],
        ["uv", "run", "--frozen", "pytest", "tests/unit", "tests/fitness", "-x", "--tb=short"],
    ):
        result = _run(cmd, cwd=worktree, timeout=900)
        if result.returncode != 0:
            print(f"GATE FAILED: {' '.join(cmd)}")
            print(result.stdout[-2000:])
            return False
    return True


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Agent Issue->PR runner")
    parser.add_argument("--issue", type=int, help="issue number (default: oldest claimed)")
    parser.add_argument("--model", default=os.environ.get("DEV_AGENT_MODEL", ""),
                        help="opencode model (provider/model)")
    parser.add_argument("--timeout", type=int, default=1800, help="agent timeout seconds")
    parser.add_argument("--execute", action="store_true",
                        help="actually run the agent (default: dry-run)")
    args = parser.parse_args(argv)

    number = resolve_issue(args.issue)
    if not number:
        print("no pending issue (claimed without PR)")
        return 0
    issue = fetch_issue(number)
    if not issue:
        print(f"issue #{number} not found")
        return 1
    if issue.get("state") != "OPEN":
        print(f"issue #{number} is {issue.get('state')} — skip")
        return 0
    if not is_auto_safe(issue):
        print(f"issue #{number} lacks the auto-safe label — skip")
        return 0

    branch = f"issue-{number}-auto"
    worktree = os.path.join(WORKTREE_ROOT, branch)
    prompt = build_prompt(issue)

    print(f"issue:    #{number} {issue['title']}")
    print(f"branch:   {branch}")
    print(f"worktree: {worktree}")
    print(f"model:    {args.model or '(opencode default)'}")
    print(f"execute:  {args.execute}")
    if not args.execute:
        print("--- prompt ---")
        print(prompt[:1500])
        return 0

    os.makedirs(WORKTREE_ROOT, exist_ok=True)
    _run(["git", "fetch", "origin", branch], cwd=REPO, timeout=120)
    _run(["git", "worktree", "remove", "--force", worktree], cwd=REPO, timeout=60)
    result = _run(
        ["git", "worktree", "add", "--force", worktree, "-B", branch, f"origin/{branch}"],
        cwd=REPO, timeout=120,
    )
    if result.returncode != 0:
        print(f"worktree add failed: {result.stderr[-500:]}")
        return 1

    try:
        rc = run_agent(worktree, prompt, args.model, args.timeout)
        if rc != 0:
            print(f"agent exited {rc}")
            return 1
        if not gate(worktree):
            print("gating failed — no PR")
            return 1
        _run(["git", "add", "-A"], cwd=worktree)
        diff = _run(["git", "status", "--porcelain"], cwd=worktree)
        if not diff.stdout.strip():
            print("no changes produced — nothing to PR")
            return 0
        _run(["git", "commit", "-m", f"feat: resolve #{number} {issue['title']}"], cwd=worktree)
        push = _run(["git", "push", "-u", "origin", branch], cwd=worktree, timeout=120)
        if push.returncode != 0:
            print(f"push failed: {push.stderr[-500:]}")
            return 1
        url = create_pr(number, DEFAULT_REPO)
        print(f"PR: {url}" if url else "PR creation failed")
        return 0 if url else 1
    finally:
        _run(["git", "worktree", "remove", "--force", worktree], cwd=REPO, timeout=60)


if __name__ == "__main__":
    sys.exit(main())

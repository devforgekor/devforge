# 부록 — AGENTS.md 동기화 재설계 가이드

- 상위 문서: `docs/plans/agents-sync-redesign-guide.md`

## 부록 A — 파일 인벤토리

| 구분 | 경로 | 처리 |
|------|------|------|
| SSOT | `docs/architecture/infrastructure.md` | 무변경(state_collector) |
| SSOT | `/home/opc/llm-common-rule.md` | 무변경 |
| SSOT | `/home/opc/llm-agent-rule.md` | **P1-1 역이관** |
| canonical | `/home/opc/AGENTS.md` | 생성기 산출(D1) — 직접 수정 금지 |
| **제거** | `/home/opc/infrastructure.md` | **P2-3 삭제**(D2) |
| **미생성** | `.github/copilot-instructions.md` | **만들지 않음**(D5, Copilot이 AGENTS.md 네이티브) |
| 수정 | `/home/opc/CLAUDE.md` | **P2-3 `@AGENTS.md` + 라우팅**(D3) |
| 참조 | `.claude/CLAUDE.md`, `server/CLAUDE.md` | 무변경 |
| (선택) | `/opt/projects/server/AGENTS.md` | **P2-8 symlink**(D10) |
| 신규 | `scripts/gen_agents.py` | P2-1 (코드: 부록 C) |
| 신규 | `systemd/agents/agents-header.md` | P2-2 |
| 신규 | `systemd/user/devforge-agents-gen.{service,path}` | P2-4 |
| 신규 | `systemd/user/devforge-agents-drift.{service,timer}` | P2-4 |
| 수정 | `scripts/deploy/sync-units.sh` | P2-5 (`*.path`) |
| 수정 | `scripts/lib/watchdog/config.py` | P2-6, Phase 3 |
| 수정 | `docs/architecture/code-structure.yaml` | P1-3 |

## 부록 B — 증거/검증 명령

```bash
# B-1. 드리프트 재현 (F2/F3)
cd /tmp && head -7 /home/opc/AGENTS.md > gen.md \
  && cat /home/opc/infrastructure.md /home/opc/llm-common-rule.md /home/opc/llm-agent-rule.md >> gen.md \
  && diff /home/opc/AGENTS.md gen.md

# B-2. 인프라 사본 (F7)
diff /home/opc/infrastructure.md /opt/projects/server/docs/architecture/infrastructure.md

# B-3. 생성기 삭제 커밋 (F1)
git -C /opt/projects/server log --oneline --diff-filter=D -- scripts/sync_gemini_rules.py

# B-4. Copilot 타깃 부재 (F6)
find /home/opc /opt/projects/server -name 'copilot-instructions.md' 2>/dev/null

# B-5. 미등록 타이머 (F12) — UNIT=$(NF-1) (핵심: $NF는 service라 항상 빈 출력)
comm -23 \
  <(systemctl --user list-timers --all --no-legend | awk '{print $(NF-1)}' | grep '\.timer$' | sort -u) \
  <(python3.12 -c "import sys;sys.path.insert(0,'/opt/projects/server/scripts');from lib.watchdog import config as c;print('\n'.join(sorted(c.TIMER_TARGETS)))")
```

---

## 부록 C — `scripts/gen_agents.py` 전체 코드

```python
#!/usr/bin/env python3
# Status: production
# Path: systemd:devforge-agents-gen.service (triggered by devforge-agents-gen.path)
"""gen_agents.py — regenerate AGENTS.md from the rule SSOT.

SSOT (sources): docs/architecture/infrastructure.md, llm-common-rule.md, llm-agent-rule.md
Canonical (never hand-edit): AGENTS.md
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
import tempfile
from pathlib import Path

log = logging.getLogger("gen_agents")
REPO = Path("/opt/projects/server")
HOME = Path.home()
HEADER = REPO / "systemd" / "agents" / "agents-header.md"
CANONICAL_INFRA = REPO / "docs" / "architecture" / "infrastructure.md"
RULE_FILES = [HOME / "llm-common-rule.md", HOME / "llm-agent-rule.md"]
AGENTS_TARGET = HOME / "AGENTS.md"


def render() -> str:
    for p in [HEADER, CANONICAL_INFRA, *RULE_FILES]:
        if not p.exists():
            raise FileNotFoundError(f"missing source: {p}")
    return "".join(p.read_text(encoding="utf-8") for p in [HEADER, CANONICAL_INFRA, *RULE_FILES])


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def atomic_write(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".agents-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _sync_file(target: Path, text: str, mode: int) -> bool:
    current = target.read_text(encoding="utf-8") if target.exists() else ""
    if _sha(text) == _sha(current):
        return False
    atomic_write(target, text, mode)
    return True


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="drift check only; exit 1 on diff")
    args = ap.parse_args(argv)

    try:
        desired = render()
    except Exception as e:
        log.error("render failed: %s", e)
        return 2

    if args.check:
        cur = AGENTS_TARGET.read_text(encoding="utf-8") if AGENTS_TARGET.exists() else ""
        if _sha(desired) != _sha(cur):
            log.error("DRIFT: %s out of sync", AGENTS_TARGET)
            return 1
        log.info("OK: %s in sync", AGENTS_TARGET)
        return 0

    changed = _sync_file(AGENTS_TARGET, desired, 0o600)
    log.info("agents sync: AGENTS.md=%s", "updated" if changed else "unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

#!/usr/bin/env python3
# Status: production
# Path: /opt/projects/server/scripts/deploy/env-file-normalize.py (kv-export-env.sh에서 호출)
"""EnvironmentFile 정규화 + quoting 오류 감시.

배경(2026-09-19): kv-fetch-env.py `env`가 과거 `KEY='value'`(shell single-quote)로
출력했다. systemd/podman EnvironmentFile 소비자는 따옴표를 값의 일부로 읽어
비밀번호·키 끝에 `'`가 붙어 인증이 조용히 실패했다(WEBOBSIDIAN_PASSWORD 사례).

동작:
  1. 감지: 값이 짝이 맞는 single/double quote로 감싸이고, 그 안에 quote가 없으면
     quoting artifact 로 판단 → 즉시 수정(자동 복구) + 경고 출력.
     (pem/인증서처럼 정당한 경우는 첫/끝 quote가 같은 문자일 때만 의심하므로,
      내부 quote가 있는 값은 건드리지 않는다)
  2. 검증: 정규화 후 KEY=VALUE 형식이 아닌 줄이 있으면 실패(비정상 전파 차단).
  3. 복구 불가한 이상값은 그대로 두되 경고를 남긴다(가시성).

사용법: env-file-normalize.py <env_file> [--check-only]
종료코드: 0 정상(자동수정 포함), 1 형식 오류로 사용 불가
"""

from __future__ import annotations

import re
import sys

LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)


def normalize_value(value: str) -> tuple[str, bool]:
    """quoted value → raw. (value, changed) 반환."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        inner = value[1:-1]
        # 내부에 같은 quote가 없을 때만 인위적 quoting으로 판단 (내용 훼손 방지)
        if value[0] not in inner:
            return inner, True
    return value, False


def normalize_text(text: str) -> tuple[str, list[str], list[str]]:
    """전체 env 텍스트를 한 줄씩 정규화. (정규화텍스트, 수정키, 형식오류줄)."""
    out: list[str] = []
    fixed: list[str] = []
    malformed: list[str] = []

    for line in text.split("\n"):
        if not line or line.startswith("#"):
            out.append(line)
            continue
        m = LINE_RE.match(line)
        if not m:
            malformed.append(line)
            out.append(line)
            continue
        key, value = m.group(1), m.group(2)
        new_value, changed = normalize_value(value)
        if changed:
            fixed.append(key)
        out.append(f"{key}={new_value}")

    return "\n".join(out), fixed, malformed


def main() -> int:
    if len(sys.argv) < 2:
        print("사용법: env-file-normalize.py <env_file> [--check-only]", file=sys.stderr)
        return 1

    path = sys.argv[1]
    check_only = "--check-only" in sys.argv[2:]

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        print(f"❌ env 파일 읽기 실패: {e}", file=sys.stderr)
        return 1

    normalized, fixed, malformed = normalize_text(text)

    if fixed and not check_only:
        with open(path, "w", encoding="utf-8") as f:
            f.write(normalized)
        print(
            f"⚠️  EnvironmentFile quoting 자동수정: {len(fixed)}개 키 "
            f"({', '.join(sorted(set(fixed)))}) — kv-fetch-env.py env 출력을 raw로 유지하세요",
            file=sys.stderr,
        )
    elif fixed and check_only:
        print(
            f"⚠️  quoting artifact 감지(미수정, check-only): {', '.join(sorted(set(fixed)))}",
            file=sys.stderr,
        )

    if malformed:
        sample = malformed[0][:40]
        print(
            f"❌ KEY=VALUE 형식 오류 {len(malformed)}줄 (예: '{sample}')", file=sys.stderr
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

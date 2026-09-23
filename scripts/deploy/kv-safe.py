#!/usr/bin/env python3
# Status: production
# Path: scripts/deploy/kv-safe.py
# Key Vault 시크릿 값을 stdout/로그에 절대 노출하지 않는 안전 래퍼.
#
# [WARNING] 이 스크립트는 시크릿 값을 출력하지 않는다. 값이 필요하면
#   0600 임시파일 경유 또는 해시 비교만 사용한다. `az keyvault secret show -o tsv`
#   를 직접 실행해 값을 화면/로그에 남기지 말 것.
#
# 서브커맨드:
#   list <vault> [prefix]              시크릿 이름만 출력
#   compare <vault> <secret> <file>    로컬 파일 값과 KV 값의 해시 일치 여부만 출력
#   set-from-env <vault> <secret> <ENV> 환경변수 값을 0600 temp 경유로 등록(값 미출력)
#   set-from-file <vault> <secret> <file> 파일 값을 등록(값 미출력)
#
# 사용 예:
#   kv-safe.py list kv-common-prod-krc DATAIMPULSE-
#   kv-safe.py compare kv-common-prod-krc DATAIMPULSE-API-KEY /path/local.txt
#   DATAIMPULSE_API_KEY='...' kv-safe.py set-from-env kv-common-prod-krc DATAIMPULSE-API-KEY DATAIMPULSE_API_KEY

import hashlib
import os
import subprocess
import sys
import tempfile


def _run(args: list[str], capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["az", *args],
        capture_output=capture,
        text=True,
        encoding="utf-8",
    )


def _sha256_short(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def cmd_list(vault: str, prefix: str = "") -> int:
    """시크릿 이름만 출력 (값 미출력)."""
    r = _run(["keyvault", "secret", "list", "--vault-name", vault, "--query", "[].name", "-o", "tsv"])
    if r.returncode != 0:
        print(r.stderr.strip(), file=sys.stderr)
        return r.returncode
    for name in (r.stdout or "").splitlines():
        if not prefix or name.startswith(prefix):
            print(name)
    return 0


def _get_value(vault: str, secret: str) -> str:
    r = _run(["keyvault", "secret", "show", "--vault-name", vault, "--name", secret, "--query", "value", "-o", "tsv"])
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "secret show failed")
    return (r.stdout or "").rstrip("\n")


def cmd_compare(vault: str, secret: str, local_file: str) -> int:
    """로컬 파일 값과 KV 값의 해시를 비교 (값 미출력)."""
    if not os.path.exists(local_file):
        print(f"로컬 파일 없음: {local_file}", file=sys.stderr)
        return 2
    with open(local_file, "r", encoding="utf-8") as f:
        local = f.read().strip()
    try:
        remote = _get_value(vault, secret)
    except RuntimeError as e:
        print(f"KV 조회 실패: {e}", file=sys.stderr)
        return 1
    same = local == remote
    print(f"{secret}: local={_sha256_short(local)} remote={_sha256_short(remote)} -> {'MATCH' if same else 'MISMATCH'}")
    return 0 if same else 3


def _set_via_file(vault: str, secret: str, value: str) -> int:
    fd, path = tempfile.mkstemp(dir="/var/tmp", prefix="kv-safe.")
    try:
        os.chmod(path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(value)
        r = _run(["keyvault", "secret", "set", "--vault-name", vault, "--name", secret, "--file", path, "-o", "none"])
        if r.returncode != 0:
            print(r.stderr.strip(), file=sys.stderr)
            return r.returncode
        print(f"{secret}: 등록 완료 (len={len(value)})")
        return 0
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def cmd_set_from_env(vault: str, secret: str, env_name: str) -> int:
    value = os.environ.get(env_name, "")
    if not value:
        print(f"환경변수 비어있음: {env_name}", file=sys.stderr)
        return 2
    return _set_via_file(vault, secret, value)


def cmd_set_from_file(vault: str, secret: str, file_path: str) -> int:
    if not os.path.exists(file_path):
        print(f"파일 없음: {file_path}", file=sys.stderr)
        return 2
    with open(file_path, "r", encoding="utf-8") as f:
        return _set_via_file(vault, secret, f.read().strip())


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    args = sys.argv[2:]
    try:
        if cmd == "list":
            return cmd_list(args[0], args[1] if len(args) > 1 else "")
        if cmd == "compare" and len(args) == 3:
            return cmd_compare(args[0], args[1], args[2])
        if cmd == "set-from-env" and len(args) == 3:
            return cmd_set_from_env(args[0], args[1], args[2])
        if cmd == "set-from-file" and len(args) == 3:
            return cmd_set_from_file(args[0], args[1], args[2])
        print(f"알 수 없는 명령/인자: {cmd} {args}", file=sys.stderr)
        print(__doc__)
        return 1
    except IndexError:
        print("인자 부족", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

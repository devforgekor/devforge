#!/usr/bin/env python3
"""
Gemini CLI wrapper with key rotation.

Loads encrypted or plaintext keys, picks the next available one in
least-used order, exports GEMINI_API_KEY, and runs gemini.

Usage:
  # Full CLI session (auto-picks key)
  /opt/projects/server/scripts/gemini_rotate.py

  # Headless mode
  /opt/projects/server/scripts/gemini_rotate.py -p "explain this code"

  # Show key stats
  /opt/projects/server/scripts/gemini_rotate.py --stats

  # Record a rate-limited key manually
  /opt/projects/server/scripts/gemini_rotate.py --rate-limited 3 60
"""

import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, "/opt/projects/server")

from lib.crypto import decrypt_data
from lib.key_rotator import KeyRotator, DAILY_QUOTA_THRESHOLD

STATE_FILE = os.path.expanduser("~/.cache/devforge/gemini_rotator_state.json")


def _load_keys() -> list[tuple[str, str]]:
    """Load Gemini API keys from secrets.env or GEMINI_API_KEYS env var."""
    secrets_path = os.path.expanduser("~/.config/devforge/secrets.env")
    keys_str = ""

    # 1. Try secrets.env
    if os.path.exists(secrets_path):
        with open(secrets_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("GEMINI_API_KEYS="):
                    keys_str = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break

    # 2. Fallback to env var
    if not keys_str:
        keys_str = os.getenv("GEMINI_API_KEYS", "")

    # 3. Fallback to single key
    if not keys_str:
        single = os.getenv("GEMINI_API_KEY", "")
        if single:
            return [("default", single)]
        return []

    keys = []
    for item in keys_str.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            name, cipher = item.split(":", 1)
            name = name.strip()
            cipher = cipher.strip()
            plain = decrypt_data(cipher)
            if plain is None:
                print(f"경고: 키 복호화 실패 — {name} (평문으로 시도)", file=sys.stderr)
                plain = cipher
            keys.append((name, plain))
        else:
            cipher = item.strip()
            plain = decrypt_data(cipher)
            if plain is None:
                plain = cipher
            keys.append((f"key-{len(keys)}", plain))

    return keys


def _load_state() -> dict:
    import json
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_state(rotator: KeyRotator):
    import json
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    state = {
        "calls": rotator._calls,
        "fails": rotator._fails,
        "last_used": rotator._last_used,
        "backoff_until": rotator._backoff_until,
    }
    with open(STATE_FILE + ".tmp", "w") as f:
        json.dump(state, f)
    os.rename(STATE_FILE + ".tmp", STATE_FILE)


def cmd_run(args):
    keys = _load_keys()
    if not keys:
        print("오류: GEMINI_API_KEYS 또는 GEMINI_API_KEY가 설정되지 않았습니다.", file=sys.stderr)
        sys.exit(1)

    rotator = KeyRotator(keys)

    # Restore state from previous runs
    state = _load_state()
    if state:
        rotator._calls = {int(k): v for k, v in state.get("calls", {}).items()}
        rotator._fails = {int(k): v for k, v in state.get("fails", {}).items()}
        rotator._last_used = {int(k): v for k, v in state.get("last_used", {}).items()}
        rotator._backoff_until = {int(k): v for k, v in state.get("backoff_until", {}).items()}

    picked = rotator.pick()
    if picked is None:
        wait = rotator.wait_seconds()
        print(f"모든 키가 백오프 상태입니다. {wait:.0f}초 후에 다시 시도하세요.", file=sys.stderr)
        sys.exit(1)

    idx, name, key = picked
    env = os.environ.copy()
    env["GEMINI_API_KEY"] = key
    env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"
    env["NODE_EXTRA_CA_CERTS"] = os.path.expanduser("~/.local/share/devforge/certs/proxy-cert.pem")

    print(f"[KeyRotator] 선택된 키: {name} ({idx+1}/{rotator.n})", file=sys.stderr)

    gemini_args = ["-m", args.model or "gemini-2.5-flash"]
    if args.prompt:
        gemini_args.extend(["-p", args.prompt])
    gemini_args.append("--skip-trust")
    gemini_args.extend(args.gemini_args)

    try:
        result = subprocess.run(["/home/opc/.local/bin/gemini"] + gemini_args, env=env)
        if result.returncode == 0:
            rotator.success(idx)
        _save_state(rotator)
        sys.exit(result.returncode)
    except FileNotFoundError:
        print("오류: gemini 명령어를 찾을 수 없습니다.", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        _save_state(rotator)
        sys.exit(130)


def cmd_stats(_args):
    keys = _load_keys()
    if not keys:
        print("등록된 키가 없습니다.")
        return

    rotator = KeyRotator(keys)
    state = _load_state()
    if state:
        rotator._calls = {int(k): v for k, v in state.get("calls", {}).items()}
        rotator._fails = {int(k): v for k, v in state.get("fails", {}).items()}
        rotator._last_used = {int(k): v for k, v in state.get("last_used", {}).items()}
        rotator._backoff_until = {int(k): v for k, v in state.get("backoff_until", {}).items()}

    stats = rotator.stats()
    print(f"키 {stats['total_keys']}개 중 {stats['available_keys']}개 사용 가능")
    print(f"총 호출: {stats['total_calls']} | 키당 평균: {stats['avg_calls_per_key']}")
    print()
    for ks in stats["keys"]:
        status = "BACKOFF" if ks["in_backoff"] else "READY"
        backoff = f" (해제까지 {ks['backoff_remaining']:.0f}초)" if ks["in_backoff"] else ""
        print(f"  [{ks['index']}] {ks['name']:<30} calls={ks['calls']:>4} fails={ks['fails']:>2}  {status}{backoff}")


def cmd_rate_limited(args):
    """Manually mark a key as rate-limited."""
    keys = _load_keys()
    if not keys:
        return
    rotator = KeyRotator(keys)
    state = _load_state()
    if state:
        rotator._calls = {int(k): v for k, v in state.get("calls", {}).items()}
        rotator._fails = {int(k): v for k, v in state.get("fails", {}).items()}
        rotator._backoff_until = {int(k): v for k, v in state.get("backoff_until", {}).items()}

    idx = args.index
    if idx < 0 or idx >= len(keys):
        print(f"오류: 유효하지 않은 인덱스 {idx} (0~{len(keys)-1})", file=sys.stderr)
        return

    rotator.rate_limited(idx, args.retry_seconds)
    _save_state(rotator)

    retry = args.retry_seconds
    if retry >= DAILY_QUOTA_THRESHOLD:
        print(f"[{idx}] {keys[idx][0]}: 일일 할당량 소진 → 다음날 KST 17시까지 격리")
    else:
        print(f"[{idx}] {keys[idx][0]}: {retry}초 백오프")


def main():
    parser = argparse.ArgumentParser(description="Gemini CLI with key rotation")
    parser.add_argument("gemini_args", nargs="*", help="Additional args passed to gemini")
    parser.add_argument("-p", "--prompt", help="Headless mode prompt")
    parser.add_argument("-m", "--model", help="Model override (default: gemini-2.5-flash)")
    parser.add_argument("--stats", action="store_true", help="Show key rotation stats")
    parser.add_argument("--rate-limited", nargs=2, metavar=("INDEX", "SECONDS"),
                        type=int, help="Mark a key as rate-limited")

    args = parser.parse_args()

    if args.stats:
        cmd_stats(args)
    elif args.rate_limited is not None:
        cmd_rate_limited(args)
    else:
        cmd_run(args)


if __name__ == "__main__":
    main()

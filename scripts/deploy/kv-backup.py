#!/usr/bin/env python3.12
# Status: production
# Path: scripts/deploy/kv-backup.py
# Key Vault → age 암호화 백업 (+ onmydoc 단방향 ship)
# - Key Vault에서 모든 시크릿 조회 → secrets.env 형식 export → age 암호화 → 로컬 저장 → onmydoc push
# - 공개키(recipient): ~/.config/devforge/backup-age-recipient (서버 보유)
# - 개인키(identity): 로컬 PC 보관 (텔레그램 세레모니 2026-09-28, 서버에는 없음)
# - [WHY] ship은 단방향 push(동기화 금지): 백업 서버가 소스를 오염시킬 수 없어야 한다
#   (backup poisoning 방지). onmydoc은 암호문만 보유하는 untrusted relay로 취급한다.
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime

HOME = os.path.expanduser("~")

TENANT_ID = os.environ.get("AZURE_KEYVAULT_TENANT_ID", "9ec65251-a106-4dc3-9878-4278caa80b1b")
CLIENT_ID = os.environ.get("AZURE_KEYVAULT_CLIENT_ID", "fcf857e3-686e-49a8-b58c-f49a33e7b840")
# 다중 KV: 앞→뒤 순서로 조회하며 동일 이름은 뒤(나중) 값이 우선한다.
KEYVAULT_URLS = [
    u.strip()
    for u in os.environ.get(
        "AZURE_KEYVAULT_URLS",
        "https://kv-common-prod-krc.vault.azure.net,https://kv-devforge-prod2-krc.vault.azure.net",
    ).split(",")
    if u.strip()
]
SECRET_FILE = os.environ.get(
    "AZURE_KEYVAULT_CLIENT_SECRET_FILE",
    os.path.join(HOME, ".config/devforge/azure-client-secret"),
)
BACKUP_DIR = os.environ.get("KV_BACKUP_DIR", os.path.join(HOME, ".config/devforge/backups"))
KEEP_DAYS = int(os.environ.get("KV_BACKUP_KEEP_DAYS", "60"))
AGE_RECIPIENT_FILE = os.environ.get(
    "AGE_RECIPIENT_FILE", os.path.join(HOME, ".config/devforge/backup-age-recipient")
)
AGE_BIN = os.environ.get("AGE_BIN", os.path.join(HOME, ".local/bin/age"))
# [WHY] 단방향 push 전용(동기화 금지) — 백업 서버가 소스를 오염시키지 못하게 한다.
# 빈 문자열이면 ship 비활성(KV_BACKUP_SHIP_HOST="").
SHIP_HOST = os.environ.get("KV_BACKUP_SHIP_HOST", "onmydoc")
SHIP_DIR = os.environ.get("KV_BACKUP_SHIP_DIR", "kv-backup")
MAX_RETRIES = 3
RETRY_BACKOFF = [1, 2, 4]  # exponential backoff (seconds)
TOKEN_CACHE_FILE = f"/run/user/{os.getuid()}/kv-token-cache.json"
TOKEN_CACHE_MARGIN = 300  # 5분 여유


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    return r


def curl_bearer_config(token):
    """curl 설정 문자열 — bearer 토큰을 argv 대신 stdin 으로 넘긴다.

    [WHY] 토큰이 argv 에 있으면 /proc/<pid>/cmdline 으로 로컬 사용자가 읽을 수 있다.
    """
    return f'header = "Authorization: Bearer {token}"\n'


def is_retryable_error(status_code, curl_exit):
    """일시적 오류 판단 (429, 5xx, 네트워크 오류)"""
    if curl_exit != 0:
        return True
    return status_code in ("429", "500", "502", "503", "504")


def load_cached_token():
    """캐시된 토큰 로드 (만료되지 않은 경우)"""
    if not os.path.exists(TOKEN_CACHE_FILE):
        return None

    try:
        with open(TOKEN_CACHE_FILE, "r") as f:
            cache = json.load(f)

        # 만료 시간 체크 (5분 여유)
        if time.time() < cache.get("expires_at", 0) - TOKEN_CACHE_MARGIN:
            return cache.get("access_token")
    except (json.JSONDecodeError, IOError, KeyError):
        pass

    return None


def save_token_cache(token, expires_in):
    """토큰 캐시 저장 (expires_in: 초 단위)"""
    try:
        cache_dir = os.path.dirname(TOKEN_CACHE_FILE)
        os.makedirs(cache_dir, exist_ok=True)

        cache = {
            "access_token": token,
            "expires_at": time.time() + expires_in,
            "cached_at": time.time(),
        }

        with open(TOKEN_CACHE_FILE, "w") as f:
            json.dump(cache, f)
        os.chmod(TOKEN_CACHE_FILE, 0o600)
    except (IOError, OSError) as e:
        # 캐시 실패는 치명적이지 않음 (경고만)
        print(f"⚠️  토큰 캐시 저장 실패: {e}", file=sys.stderr)


def get_token():
    # 캐시 확인
    cached = load_cached_token()
    if cached:
        print("✅ 캐시된 Azure 토큰 사용")
        return cached

    if not os.path.exists(SECRET_FILE):
        print("❌ client secret 파일 없음:", SECRET_FILE, file=sys.stderr)
        sys.exit(1)
    client_secret = open(SECRET_FILE).read().strip()

    for attempt in range(MAX_RETRIES):
        r = run(
            [
                "curl",
                "-s",
                "-w",
                "\n%{http_code}",
                "-X",
                "POST",
                f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token",
                "-H",
                "Content-Type: application/x-www-form-urlencoded",
                "--data-binary",
                "@-",
            ],
            # [WHY] client_secret 을 argv 에 두면 /proc/<pid>/cmdline 으로 노출된다.
            input=(
                "grant_type=client_credentials"
                f"&client_id={CLIENT_ID}&client_secret={client_secret}"
                "&scope=https://vault.azure.net/.default"
            ),
        )

        lines = r.stdout.strip().split("\n") if r.returncode == 0 else []
        body = "\n".join(lines[:-1]) if len(lines) > 1 else ""
        status = lines[-1] if lines else "000"

        if is_retryable_error(status, r.returncode):
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_BACKOFF[attempt]
                print(
                    f"⚠️  토큰 획득 실패 (curl:{r.returncode}, HTTP {status}), {delay}초 후 재시도 ({attempt + 1}/{MAX_RETRIES})",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            else:
                print(f"❌ 토큰 획득 실패 (최대 재시도 초과): {body[:300]}", file=sys.stderr)
                sys.exit(1)

        if r.returncode != 0:
            print(f"❌ curl 실행 실패 (exit {r.returncode}): {r.stderr}", file=sys.stderr)
            sys.exit(1)

        if not status.startswith("2"):
            print(f"❌ 토큰 획득 실패 (HTTP {status}): {body[:300]}", file=sys.stderr)
            sys.exit(1)

        try:
            token_response = json.loads(body)
            access_token = token_response["access_token"]
            expires_in = token_response.get("expires_in", 3600)  # 기본 1시간

            # 캐시 저장
            save_token_cache(access_token, expires_in)
            print(f"✅ 새 Azure 토큰 획득 (유효 시간: {expires_in}초)")

            return access_token
        except (json.JSONDecodeError, KeyError) as e:
            print(f"❌ 토큰 응답 파싱 실패: {e}\n{body[:300]}", file=sys.stderr)
            sys.exit(1)


def list_secrets(token, vault_url):
    secrets = []
    url = f"{vault_url}/secrets?api-version=7.4"
    while url:
        success = False
        for attempt in range(MAX_RETRIES):
            r = run(
                ["curl", "-s", "-w", "\n%{http_code}", url, "--config", "-"],
                input=curl_bearer_config(token),
            )

            lines = r.stdout.strip().split("\n") if r.returncode == 0 else []
            body = "\n".join(lines[:-1]) if len(lines) > 1 else ""
            status = lines[-1] if lines else "000"

            if is_retryable_error(status, r.returncode):
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_BACKOFF[attempt]
                    print(
                        f"⚠️  시크릿 목록 조회 실패 (curl:{r.returncode}, HTTP {status}), {delay}초 후 재시도 ({attempt + 1}/{MAX_RETRIES})",
                        file=sys.stderr,
                    )
                    time.sleep(delay)
                    continue
                else:
                    print(
                        f"❌ 시크릿 목록 조회 실패 (최대 재시도 초과): {body[:300]}",
                        file=sys.stderr,
                    )
                    sys.exit(1)

            if r.returncode != 0:
                print(f"❌ curl 실행 실패 (exit {r.returncode}): {r.stderr}", file=sys.stderr)
                sys.exit(1)

            if not status.startswith("2"):
                print(f"❌ 시크릿 목록 조회 실패 (HTTP {status}): {body[:300]}", file=sys.stderr)
                sys.exit(1)

            try:
                data = json.loads(body)
                secrets.extend(s["id"].split("/")[-1] for s in data.get("value", []))
                url = data.get("nextLink", "")
                success = True
                break
            except json.JSONDecodeError as e:
                print(f"❌ 시크릿 목록 파싱 실패: {e}\n{body[:300]}", file=sys.stderr)
                sys.exit(1)

        if not success:
            print("❌ 시크릿 목록 조회 실패", file=sys.stderr)
            sys.exit(1)

    return secrets


def get_secret_value(token, vault_url, name):
    r = run(
        [
            "curl",
            "-s",
            "-w",
            "\n%{http_code}",
            f"{vault_url}/secrets/{name}?api-version=7.4",
            "--config",
            "-",
        ],
        input=curl_bearer_config(token),
    )
    if r.returncode != 0:
        print(
            f"⚠️  시크릿 '{name}' 조회 실패 (curl exit {r.returncode}): {r.stderr}", file=sys.stderr
        )
        return ""

    lines = r.stdout.strip().split("\n")
    body = "\n".join(lines[:-1])
    status = lines[-1] if lines else "000"

    if not status.startswith("2"):
        print(f"⚠️  시크릿 '{name}' 조회 실패 (HTTP {status}): {body[:200]}", file=sys.stderr)
        return ""

    try:
        data = json.loads(body)
        return data.get("value", "")
    except (json.JSONDecodeError, KeyError) as e:
        print(f"⚠️  시크릿 '{name}' 파싱 실패: {e}", file=sys.stderr)
        return ""


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ship(out_file: str) -> None:
    """암호문을 SHIP_HOST로 단방향 push하고 원격 무결성 검증/회전. 동기화는 하지 않는다."""
    if not SHIP_HOST:
        print("⏭️  ship 비활성 (KV_BACKUP_SHIP_HOST 없음)")
        return

    basename = os.path.basename(out_file)
    sidecar = out_file + ".sha256"
    with open(sidecar, "w") as f:
        f.write(f"{sha256_file(out_file)}  {basename}\n")
    os.chmod(sidecar, 0o600)

    r = run(["ssh", "-o", "ConnectTimeout=15", SHIP_HOST, f"mkdir -p ~/{SHIP_DIR}"])
    if r.returncode != 0:
        print(f"❌ ship 디렉토리 준비 실패: {r.stderr.strip()[:300]}", file=sys.stderr)
        sys.exit(1)

    r = run(["scp", out_file, sidecar, f"{SHIP_HOST}:{SHIP_DIR}/"])
    if r.returncode != 0:
        print(f"❌ ship 전송 실패: {r.stderr.strip()[:300]}", file=sys.stderr)
        sys.exit(1)

    r = run(
        [
            "ssh",
            "-o",
            "ConnectTimeout=15",
            SHIP_HOST,
            f"cd ~/{SHIP_DIR} && sha256sum -c {basename}.sha256",
        ]
    )
    if r.returncode != 0:
        print(f"❌ ship 무결성 검증 실패: {(r.stdout + r.stderr).strip()[:300]}", file=sys.stderr)
        sys.exit(1)

    r = run(
        [
            "ssh",
            "-o",
            "ConnectTimeout=15",
            SHIP_HOST,
            f"find ~/{SHIP_DIR} -name 'secrets-backup-*' -mtime +{KEEP_DAYS} -delete",
        ]
    )
    if r.returncode != 0:
        print(f"⚠️  ship 원격 prune 실패(비치명): {r.stderr.strip()[:200]}", file=sys.stderr)
    print(f"📤 ship 완료: {SHIP_HOST}:~/{SHIP_DIR}/{basename}")


def main():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    token = get_token()
    print("✅ Azure 토큰 획득")

    # 다중 KV 병합 (뒤 KV가 동일 이름을 덮어씀)
    merged = {}
    for vault_url in KEYVAULT_URLS:
        for name in list_secrets(token, vault_url):
            merged[name] = vault_url
    print(f"📋 Key Vault에서 {len(merged)}개 시크릿 발견")

    # env 파일 구성 (하이픈 → 밑줄 복원)
    env_lines = []
    for name, vault_url in merged.items():
        underscore_name = name.replace("-", "_")
        value = get_secret_value(token, vault_url, name)
        env_lines.append(f"{underscore_name}={value}")
        print(f"  ✓ {underscore_name}")

    # 임시 파일 생성 (안전한 디렉토리, mode 0600)
    with tempfile.NamedTemporaryFile(mode="w", delete=False, dir=BACKUP_DIR, suffix=".env") as f:
        tmp_env = f.name
        f.write("\n".join(env_lines) + "\n")

    try:
        # age 암호화 — recipient(공개키)만 서버 보유, identity는 로컬 PC
        if not os.path.exists(AGE_RECIPIENT_FILE):
            print("❌ age recipient 없음:", AGE_RECIPIENT_FILE, file=sys.stderr)
            sys.exit(1)
        with open(AGE_RECIPIENT_FILE) as f:
            recipient = f.read().strip()

        utc_timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        out_file = os.path.join(BACKUP_DIR, f"secrets-backup-{utc_timestamp}.age")
        r = run([AGE_BIN, "-r", recipient, "-o", out_file, tmp_env])
        if r.returncode != 0 or not os.path.exists(out_file):
            print("❌ age 암호화 실패:", r.stderr, file=sys.stderr)
            sys.exit(1)

        os.chmod(out_file, 0o600)
        size = os.path.getsize(out_file)
        print(f"✅ 백업 완료: {out_file} ({size / 1024:.1f} KB)")
        ship(out_file)
    finally:
        # 임시 파일 안전하게 삭제
        if os.path.exists(tmp_env):
            os.remove(tmp_env)

    # 오래된 백업 정리 (.gpg 구세대 포함, 로컬·원격 공통 KEEP_DAYS)
    cutoff = time.time() - KEEP_DAYS * 86400
    for fn in os.listdir(BACKUP_DIR):
        if not fn.startswith("secrets-backup-"):
            continue
        if not fn.endswith((".gpg", ".age", ".age.sha256")):
            continue
        fp = os.path.join(BACKUP_DIR, fn)
        if os.path.getmtime(fp) < cutoff:
            os.remove(fp)
            print(f"🗑️  삭제: {fn}")

    print("📦 백업 파일 목록:")
    for fn in sorted(os.listdir(BACKUP_DIR)):
        if fn.endswith((".gpg", ".age")):
            print(f"  {fn} ({os.path.getsize(os.path.join(BACKUP_DIR, fn)) / 1024:.1f} KB)")


if __name__ == "__main__":
    main()

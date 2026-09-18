#!/usr/bin/env python3
# Status: production
# Path: scripts/deploy/kv-backup.py
# Key Vault → GPG 암호화 백업
# - Key Vault에서 모든 시크릿 조회 → secrets.env 형식 export → GPG 암호화 → 저장
# - 공개키: ~/.config/devforge/backup-public-key.asc (서버 보유)
# - 개인키: 로컬 PC 보관 (복호화는 로컬에서만)
import json
import os
import subprocess
import sys
import time
from datetime import datetime

HOME = os.path.expanduser("~")

KEYVAULT_URL = os.environ.get(
    "AZURE_MESIDS_KEYVAULT_URL", "https://kv-devforge-prod-krc.vault.azure.net"
)
TENANT_ID = os.environ.get("AZURE_MESIDS_TENANT_ID", "b08cd1bf-7952-489c-8fbb-aa907bb74709")
CLIENT_ID = os.environ.get("AZURE_MESIDS_CLIENT_SECRET_ID", "169a8e1e-9bd1-4023-a78a-785e2fec321d")
SECRET_FILE = os.path.join(HOME, ".config/devforge/azure-client-secret")
GPG_RECIPIENT = "DevForge Secrets Backup"
BACKUP_DIR = os.environ.get("KV_BACKUP_DIR", os.path.join(HOME, ".config/devforge/backups"))
KEEP_DAYS = int(os.environ.get("KV_BACKUP_KEEP_DAYS", "60"))
PUBLIC_KEY_FILE = os.path.join(HOME, ".config/devforge/backup-public-key.asc")
MAX_RETRIES = 3
RETRY_BACKOFF = [1, 2, 4]  # exponential backoff (seconds)


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    return r


def is_retryable_error(status_code, curl_exit):
    """일시적 오류 판단 (429, 5xx, 네트워크 오류)"""
    if curl_exit != 0:
        return True
    if status_code in ("429", "500", "502", "503", "504"):
        return True
    return False


def get_token():
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
                "-d",
                "grant_type=client_credentials",
                "-d",
                f"client_id={CLIENT_ID}",
                "-d",
                f"client_secret={client_secret}",
                "-d",
                "scope=https://vault.azure.net/.default",
            ]
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
            return json.loads(body)["access_token"]
        except (json.JSONDecodeError, KeyError) as e:
            print(f"❌ 토큰 응답 파싱 실패: {e}\n{body[:300]}", file=sys.stderr)
            sys.exit(1)


def list_secrets(token):
    secrets = []
    url = f"{KEYVAULT_URL}/secrets?api-version=7.4"
    while url:
        success = False
        for attempt in range(MAX_RETRIES):
            r = run(
                ["curl", "-s", "-w", "\n%{http_code}", url, "-H", f"Authorization: Bearer {token}"]
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


def get_secret_value(token, name):
    r = run(
        [
            "curl",
            "-s",
            "-w",
            "\n%{http_code}",
            f"{KEYVAULT_URL}/secrets/{name}?api-version=7.4",
            "-H",
            f"Authorization: Bearer {token}",
        ]
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


def main():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    token = get_token()
    print("✅ Azure 토큰 획득")

    secrets = list_secrets(token)
    print(f"📋 Key Vault에서 {len(secrets)}개 시크릿 발견")

    # env 파일 구성 (하이픈 → 밑줄 복원)
    env_lines = []
    for name in secrets:
        underscore_name = name.replace("-", "_")
        value = get_secret_value(token, name)
        env_lines.append(f"{underscore_name}={value}")
        print(f"  ✓ {underscore_name}")

    # 임시 파일 생성 (안전한 디렉토리, mode 0600)
    with tempfile.NamedTemporaryFile(mode="w", delete=False, dir=BACKUP_DIR, suffix=".env") as f:
        tmp_env = f.name
        f.write("\n".join(env_lines) + "\n")

    try:
        # GPG 암호화
        if not os.path.exists(PUBLIC_KEY_FILE):
            print("❌ GPG 공개키 없음:", PUBLIC_KEY_FILE, file=sys.stderr)
            sys.exit(1)
        run(["gpg", "--import", PUBLIC_KEY_FILE])

        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        out_file = os.path.join(BACKUP_DIR, f"secrets-backup-{ts}.gpg")
        r = run(
            [
                "gpg",
                "--batch",
                "--yes",
                "--recipient",
                GPG_RECIPIENT,
                "--trust-model",
                "always",
                "--encrypt",
                "--output",
                out_file,
                tmp_env,
            ]
        )
        if r.returncode != 0 or not os.path.exists(out_file):
            print("❌ GPG 암호화 실패:", r.stderr, file=sys.stderr)
            sys.exit(1)

        os.chmod(out_file, 0o600)
        size = os.path.getsize(out_file)
        print(f"✅ 백업 완료: {out_file} ({size / 1024:.1f} KB)")
    finally:
        # 임시 파일 안전하게 삭제
        if os.path.exists(tmp_env):
            os.remove(tmp_env)

    # 오래된 백업 정리
    cutoff = time.time() - KEEP_DAYS * 86400
    for fn in os.listdir(BACKUP_DIR):
        if not fn.startswith("secrets-backup-") or not fn.endswith(".gpg"):
            continue
        fp = os.path.join(BACKUP_DIR, fn)
        if os.path.getmtime(fp) < cutoff:
            os.remove(fp)
            print(f"🗑️  삭제: {fn}")

    print("📦 백업 파일 목록:")
    for fn in sorted(os.listdir(BACKUP_DIR)):
        if fn.endswith(".gpg"):
            print(f"  {fn} ({os.path.getsize(os.path.join(BACKUP_DIR, fn)) / 1024:.1f} KB)")


if __name__ == "__main__":
    main()

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
TENANT_ID = os.environ.get(
    "AZURE_MESIDS_TENANT_ID", "b08cd1bf-7952-489c-8fbb-aa907bb74709"
)
CLIENT_ID = os.environ.get(
    "AZURE_MESIDS_CLIENT_SECRET_ID", "169a8e1e-9bd1-4023-a78a-785e2fec321d"
)
SECRET_FILE = os.path.join(HOME, ".config/devforge/azure-client-secret")
GPG_RECIPIENT = "DevForge Secrets Backup"
BACKUP_DIR = os.environ.get(
    "KV_BACKUP_DIR", os.path.join(HOME, ".config/devforge/backups")
)
KEEP_DAYS = int(os.environ.get("KV_BACKUP_KEEP_DAYS", "60"))
PUBLIC_KEY_FILE = os.path.join(HOME, ".config/devforge/backup-public-key.asc")


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    return r


def get_token():
    if not os.path.exists(SECRET_FILE):
        print("❌ client secret 파일 없음:", SECRET_FILE, file=sys.stderr)
        sys.exit(1)
    client_secret = open(SECRET_FILE).read().strip()
    r = run(
        [
            "curl", "-s", "-X", "POST",
            f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token",
            "-d", "grant_type=client_credentials",
            "-d", f"client_id={CLIENT_ID}",
            "-d", f"client_secret={client_secret}",
            "-d", "scope=https://vault.azure.net/.default",
        ]
    )
    try:
        return json.loads(r.stdout)["access_token"]
    except Exception:
        print("❌ 토큰 획득 실패:", r.stdout[:300], file=sys.stderr)
        sys.exit(1)


def list_secrets(token):
    secrets = []
    url = f"{KEYVAULT_URL}/secrets?api-version=7.4"
    while url:
        r = run(["curl", "-s", url, "-H", f"Authorization: Bearer {token}"])
        data = json.loads(r.stdout)
        secrets.extend(s["id"].split("/")[-1] for s in data.get("value", []))
        url = data.get("nextLink", "")
    return secrets


def get_secret_value(token, name):
    r = run(
        [
            "curl", "-s",
            f"{KEYVAULT_URL}/secrets/{name}?api-version=7.4",
            "-H", f"Authorization: Bearer {token}",
        ]
    )
    try:
        return json.loads(r.stdout).get("value", "")
    except Exception:
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

    tmp_env = os.path.join("/tmp", f"kv-export-{os.getpid()}.env")
    with open(tmp_env, "w") as f:
        f.write("\n".join(env_lines) + "\n")

    # GPG 암호화
    if not os.path.exists(PUBLIC_KEY_FILE):
        print("❌ GPG 공개키 없음:", PUBLIC_KEY_FILE, file=sys.stderr)
        sys.exit(1)
    run(["gpg", "--import", PUBLIC_KEY_FILE])

    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    out_file = os.path.join(BACKUP_DIR, f"secrets-backup-{ts}.gpg")
    r = run(
        [
            "gpg", "--batch", "--yes", "--recipient", GPG_RECIPIENT,
            "--trust-model", "always",
            "--encrypt", "--output", out_file, tmp_env,
        ]
    )
    if r.returncode != 0 or not os.path.exists(out_file):
        print("❌ GPG 암호화 실패:", r.stderr, file=sys.stderr)
        sys.exit(1)

    os.chmod(out_file, 0o600)
    os.remove(tmp_env)
    size = os.path.getsize(out_file)
    print(f"✅ 백업 완료: {out_file} ({size/1024:.1f} KB)")

    # 오래된 백업 정리
    cutoff = time.time() - KEEP_DAYS * 86400
    for fn in os.listdir(BACKUP_DIR):
        if not fn.startswith("secrets-backup-") or not fn.endswith(".gpg"):
            continue
        fp = os.path.join(BACKUP_DIR, fn)
        if os.path.getmtime(fp) < cutoff:
            os.remove(fp)
            print(f"🗑️  삭제: {fn}")

    print(f"📦 백업 파일 목록:")
    for fn in sorted(os.listdir(BACKUP_DIR)):
        if fn.endswith(".gpg"):
            print(f"  {fn} ({os.path.getsize(os.path.join(BACKUP_DIR, fn))/1024:.1f} KB)")


if __name__ == "__main__":
    main()
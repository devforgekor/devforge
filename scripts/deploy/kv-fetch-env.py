#!/usr/bin/env python3
# Status: production
# Path: scripts/deploy/kv-fetch-env.py
# Azure Key Vault에서 시크릿을 조회해 환경변수로 설정하고 대상 명령을 실행한다.
# - 디스크에 평문 시크릿을 저장하지 않는다 (환경변수는 프로세스 메모리에만 존재)
# - 사용법: kv-fetch-env.py <command> [args...]
import json
import os
import subprocess
import sys

HOME = os.path.expanduser("~")
SECRET_FILE = os.path.join(HOME, ".config/devforge/azure-client-secret")
TENANT_ID = os.environ.get(
    "AZURE_MESIDS_TENANT_ID", "b08cd1bf-7952-489c-8fbb-aa907bb74709"
)
CLIENT_ID = os.environ.get(
    "AZURE_MESIDS_CLIENT_SECRET_ID", "169a8e1e-9bd1-4023-a78a-785e2fec321d"
)
KEYVAULT_URL = os.environ.get(
    "AZURE_MESIDS_KEYVAULT_URL", "https://kv-devforge-prod-krc.vault.azure.net"
)


def get_token():
    if not os.path.exists(SECRET_FILE):
        print(f"❌ client secret 파일 없음: {SECRET_FILE}", file=sys.stderr)
        sys.exit(1)
    client_secret = open(SECRET_FILE).read().strip()
    r = subprocess.run(
        [
            "curl", "-s", "-X", "POST",
            f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token",
            "-d", "grant_type=client_credentials",
            "-d", f"client_id={CLIENT_ID}",
            "-d", f"client_secret={client_secret}",
            "-d", "scope=https://vault.azure.net/.default",
        ],
        capture_output=True, text=True,
    )
    try:
        return json.loads(r.stdout)["access_token"]
    except Exception:
        print(f"❌ 토큰 획득 실패: {r.stdout[:300]}", file=sys.stderr)
        sys.exit(1)


def list_secrets(token):
    secrets = []
    url = f"{KEYVAULT_URL}/secrets?api-version=7.4"
    while url:
        r = subprocess.run(
            ["curl", "-s", url, "-H", f"Authorization: Bearer {token}"],
            capture_output=True, text=True,
        )
        data = json.loads(r.stdout)
        secrets.extend(s["id"].split("/")[-1] for s in data.get("value", []))
        url = data.get("nextLink", "")
    return secrets


def get_secret_value(token, name):
    r = subprocess.run(
        [
            "curl", "-s",
            f"{KEYVAULT_URL}/secrets/{name}?api-version=7.4",
            "-H", f"Authorization: Bearer {token}",
        ],
        capture_output=True, text=True,
    )
    try:
        return json.loads(r.stdout).get("value", "")
    except Exception:
        return ""


def main():
    if len(sys.argv) < 2:
        print("사용법: kv-fetch-env.py <command> [args...]", file=sys.stderr)
        sys.exit(1)

    token = get_token()
    secrets = list_secrets(token)

    for kv_name in secrets:
        env_name = kv_name.replace("-", "_")
        value = get_secret_value(token, kv_name)
        os.environ[env_name] = value

    print(f"✅ Key Vault 시크릿 로드 완료: {len(secrets)}개", file=sys.stderr)
    os.execvpe(sys.argv[1], sys.argv[1:], os.environ)


if __name__ == "__main__":
    main()
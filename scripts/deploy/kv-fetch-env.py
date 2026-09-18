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
TENANT_ID = os.environ.get("AZURE_MESIDS_TENANT_ID", "b08cd1bf-7952-489c-8fbb-aa907bb74709")
CLIENT_ID = os.environ.get("AZURE_MESIDS_CLIENT_SECRET_ID", "169a8e1e-9bd1-4023-a78a-785e2fec321d")
KEYVAULT_URL = os.environ.get(
    "AZURE_MESIDS_KEYVAULT_URL", "https://kv-devforge-prod-krc.vault.azure.net"
)
MAX_RETRIES = 3
RETRY_BACKOFF = [1, 2, 4]  # exponential backoff (seconds)


def is_retryable_error(status_code, curl_exit):
    """일시적 오류 판단 (429, 5xx, 네트워크 오류)"""
    if curl_exit != 0:
        return True
    if status_code in ("429", "500", "502", "503", "504"):
        return True
    return False


def get_token():
    if not os.path.exists(SECRET_FILE):
        print(f"❌ client secret 파일 없음: {SECRET_FILE}", file=sys.stderr)
        sys.exit(1)
    client_secret = open(SECRET_FILE).read().strip()

    for attempt in range(MAX_RETRIES):
        r = subprocess.run(
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
            ],
            capture_output=True,
            text=True,
        )

        lines = r.stdout.strip().split("\n") if r.returncode == 0 else []
        body = "\n".join(lines[:-1]) if len(lines) > 1 else ""
        status = lines[-1] if lines else "000"

        # 재시도 가능한 오류인지 판단
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
                print(
                    f"❌ 토큰 획득 실패 (최대 재시도 초과, curl:{r.returncode}, HTTP {status}): {body[:300]}",
                    file=sys.stderr,
                )
                sys.exit(1)

        # 재시도 불가능한 오류 (4xx 인증 오류 등)
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
            r = subprocess.run(
                ["curl", "-s", "-w", "\n%{http_code}", url, "-H", f"Authorization: Bearer {token}"],
                capture_output=True,
                text=True,
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
    r = subprocess.run(
        [
            "curl",
            "-s",
            "-w",
            "\n%{http_code}",
            f"{KEYVAULT_URL}/secrets/{name}?api-version=7.4",
            "-H",
            f"Authorization: Bearer {token}",
        ],
        capture_output=True,
        text=True,
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

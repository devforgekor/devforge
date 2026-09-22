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
import time

HOME = os.path.expanduser("~")
SECRET_FILE = os.environ.get(
    "AZURE_KEYVAULT_CLIENT_SECRET_FILE",
    os.path.join(HOME, ".config/devforge/azure-client-secret"),
)
TENANT_ID = os.environ.get("AZURE_KEYVAULT_TENANT_ID", "9ec65251-a106-4dc3-9878-4278caa80b1b")
CLIENT_ID = os.environ.get("AZURE_KEYVAULT_CLIENT_ID", "abc5aab0-5394-46e0-bf4d-daf4129d1d78")
# 다중 KV: 앞→뒤 순서로 조회하며 동일 이름은 뒤(나중) 값이 우선한다.
KEYVAULT_URLS = [
    u.strip()
    for u in os.environ.get(
        "AZURE_KEYVAULT_URLS",
        "https://kv-common-prod-krc.vault.azure.net,"
        "https://kv-devforge-prod2-krc.vault.azure.net",
    ).split(",")
    if u.strip()
]
MAX_RETRIES = 3
RETRY_BACKOFF = [1, 2, 4]  # exponential backoff (seconds)
TOKEN_CACHE_FILE = f"/run/user/{os.getuid()}/kv-token-cache.json"
TOKEN_CACHE_MARGIN = 300  # 5분 여유 (3600s 토큰이면 55분까지 사용)


def is_retryable_error(status_code, curl_exit):
    """일시적 오류 판단 (429, 5xx, 네트워크 오류)"""
    if curl_exit != 0:
        return True
    if status_code in ("429", "500", "502", "503", "504"):
        return True
    return False


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
        print("✅ 캐시된 Azure 토큰 사용", file=sys.stderr)
        return cached

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
            token_response = json.loads(body)
            access_token = token_response["access_token"]
            expires_in = token_response.get("expires_in", 3600)  # 기본 1시간

            # 캐시 저장
            save_token_cache(access_token, expires_in)
            print(f"✅ 새 Azure 토큰 획득 (유효 시간: {expires_in}초)", file=sys.stderr)

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


def get_secret_value(token, vault_url, name):
    r = subprocess.run(
        [
            "curl",
            "-s",
            "-w",
            "\n%{http_code}",
            f"{vault_url}/secrets/{name}?api-version=7.4",
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


def parse_selection(argv):
    """--keys KEY1,KEY2 (KV 하이픈 표기) 선택 파싱. 없으면 (None, 나머지 args)."""
    selected = None
    rest = []
    i = 0
    while i < len(argv):
        if argv[i] == "--keys" and i + 1 < len(argv):
            selected = {
                k.strip().replace("_", "-").upper()
                for k in argv[i + 1].split(",")
                if k.strip()
            }
            i += 2
        else:
            rest.append(argv[i])
            i += 1
    return selected, rest


def resolve_secrets(token, selected):
    """다중 KV를 병합해 {kv_name: vault_url} 을 반환. 뒤 KV가 동일 이름을 덮어쓴다.
    선택 키(--keys)가 어느 KV에도 없으면 즉시 실패(조용한 누락 방지)."""
    merged = {}
    for vault_url in KEYVAULT_URLS:
        for name in list_secrets(token, vault_url):
            merged[name] = vault_url
    if selected is not None:
        available = {n.upper() for n in merged}
        missing = sorted(selected - available)
        if missing:
            print(
                f"❌ KV에 없는 키 요청: {', '.join(missing)} (오타 또는 미등록)",
                file=sys.stderr,
            )
            sys.exit(1)
        merged = {n: u for n, u in merged.items() if n.upper() in selected}
    return merged


def main():
    if len(sys.argv) < 2:
        print("사용법: kv-fetch-env.py <command> [args...]", file=sys.stderr)
        print("       kv-fetch-env.py env [--keys KEY1,KEY2]  # stdout에 KEY=VALUE 출력", file=sys.stderr)
        sys.exit(1)

    # env 서브커맨드: stdout에 KEY=VALUE 형식으로 출력
    if sys.argv[1] == "env":
        selected, _ = parse_selection(sys.argv[2:])
        token = get_token()
        secrets = resolve_secrets(token, selected)

        for kv_name, vault_url in secrets.items():
            env_name = kv_name.replace("-", "_")
            value = get_secret_value(token, vault_url, kv_name)
            if value:
                # systemd EnvironmentFile 형식: 값은 그대로(raw), 개행만 제거.
                # single-quote로 감싸면 소비자(EnvironmentFile)가 따옴표를 값의
                # 일부로 읽어 인증 실패한다(2026-09-19 WEBOBSIDIAN_PASSWORD 사례).
                print(f"{env_name}={value.replace(chr(10), ' ')}")

        print(f"✅ Key Vault 시크릿 출력 완료: {len(secrets)}개", file=sys.stderr)
        sys.exit(0)

    # 기존 동작: 환경변수 주입 후 명령 실행
    # 기존 동작: 환경변수 주입 후 명령 실행
    # [WHY] rest(명령+인자)에서 --keys를 제거해 대상 명령에 새어나가지 않게 한다.
    selected, rest = parse_selection(sys.argv[1:])
    if not rest:
        print("❌ 실행할 명령이 없습니다 (사용법: kv-fetch-env.py <command> [args] [--keys K1,K2])", file=sys.stderr)
        sys.exit(1)
    token = get_token()
    secrets = resolve_secrets(token, selected)

    for kv_name, vault_url in secrets.items():
        env_name = kv_name.replace("-", "_")
        value = get_secret_value(token, vault_url, kv_name)
        os.environ[env_name] = value

    print(f"✅ Key Vault 시크릿 로드 완료: {len(secrets)}개", file=sys.stderr)
    os.execvpe(rest[0], rest, os.environ)


if __name__ == "__main__":
    main()

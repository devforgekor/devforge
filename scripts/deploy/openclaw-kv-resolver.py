#!/usr/bin/env python3.12
# Status: production
# Path: openclaw-gateway.service (secrets.providers exec resolver) — see docs/handover-secrets-kv.md
"""OpenClaw exec-provider SecretRef resolver backed by Azure Key Vault.

[WHY] The gateway token must not sit in openclaw.json plaintext or in an
EnvironmentFile on disk (AGENTS.md §0). OpenClaw's exec SecretRef protocol
lets the Gateway resolve it from Azure KV at startup, matching the repo's
kv-fetch-env.py philosophy: the value only ever lives in process memory.

Protocol (stdin -> stdout), SecretRef protocolVersion 1:
  in : {"protocolVersion":1,"ids":["<kv-secret-name>",...]}
  out: {"protocolVersion":1,"values":{"<kv-secret-name>":"<value>"},"errors":{}}

id 문법 — 두 가지:
  <kv-secret-name>                  평문 값 전체 (기존 동작, 변함 없음)
  <kv-secret-name>#path.to.key      JSON 값 안의 경로만 추출
[WHY] 토큰을 별도 시크릿으로 복제하면 로테이션이 두 곳이 된다. 경로 하나로
단일 소스(예: TELEGRAM-ENV-CONFIG#bots.alert_bot.token)를 유지한다.
Azure KV 시크릿 이름은 [A-Za-z0-9-] 만 허용하므로 `#` 는 구분자로 안전하다.
"""

import importlib.util
import json
import os
import sys

PATH_SEPARATOR = "#"

_DEPLOY_DIR = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "kv_fetch_env", os.path.join(_DEPLOY_DIR, "kv-fetch-env.py")
)
_kv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_kv)


def split_secret_id(secret_id):
    """`<name>#path.to.key` 를 (조회할 시크릿 이름, 경로|None) 로 분리한다.

    경로를 지정하지 않으면 기존 평문 id 동작과 완전히 동일하다.
    """
    if PATH_SEPARATOR not in secret_id:
        return secret_id.strip(), None
    name, _, raw_path = secret_id.partition(PATH_SEPARATOR)
    name = name.strip()
    path = raw_path.strip().strip(".")
    if not name:
        raise ValueError(f"경로를 지정했지만 시크릿 이름이 비어 있습니다: {secret_id!r}")
    if not path:
        raise ValueError(f"경로가 비어 있습니다: {secret_id!r}")
    return name, path


def extract_json_path(secret_id, value, path):
    """JSON 문자열 value 에서 점 경로(path) 의 문자열 값을 꺼낸다.

    [WHY] 값 자체는 출력·기록하지 않는다. 실패 메시지는 id·경로만 담는다.
    """
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{secret_id}: 값이 JSON 이 아니어서 경로 조회가 불가합니다") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{secret_id}: 값이 JSON 객체가 아니어서 경로 조회가 불가합니다")
    current = payload
    walked = []
    for segment in path.split("."):
        walked.append(segment)
        if not isinstance(current, dict) or segment not in current:
            raise ValueError(f"{secret_id}: 경로 {'.'.join(walked)} 를 찾지 못했습니다")
        current = current[segment]
    if not isinstance(current, str):
        raise ValueError(f"{secret_id}: 경로 {path} 의 값이 문자열이 아닙니다")
    return current


def read_request():
    raw = sys.stdin.read()
    try:
        request = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"request JSON 파싱 실패: {exc}") from exc
    if request.get("protocolVersion") != 1:
        raise ValueError("지원하지 않는 protocolVersion")
    ids = request.get("ids")
    if not isinstance(ids, list):
        raise ValueError("ids 배열이 없습니다")
    return [i for i in ids if isinstance(i, str) and i]


def resolve(names):
    if not names:
        return {}, {}
    token = _kv.get_token()
    vault_urls = _kv.KEYVAULT_URLS
    values = {}
    errors = {}
    for secret_id in names:
        try:
            name, path = split_secret_id(secret_id)
        except ValueError as exc:
            errors[secret_id] = str(exc)
            continue
        found = None
        for vault_url in vault_urls:
            value = _kv.get_secret_value(token, vault_url, name)
            if value:
                found = value
                break
        if found is None:
            errors[secret_id] = f"'{name}' 를 어떤 Key Vault에서도 찾지 못했습니다"
            continue
        if path is None:
            values[secret_id] = found
            continue
        try:
            values[secret_id] = extract_json_path(secret_id, found, path)
        except ValueError as exc:
            errors[secret_id] = str(exc)
    return values, errors


def main():
    names = read_request()
    values, errors = resolve(names)
    sys.stdout.write(json.dumps({"protocolVersion": 1, "values": values, "errors": errors}))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())

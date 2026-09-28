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
"""

import importlib.util
import json
import os
import sys

_DEPLOY_DIR = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "kv_fetch_env", os.path.join(_DEPLOY_DIR, "kv-fetch-env.py")
)
_kv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_kv)


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
    for name in names:
        found = None
        for vault_url in vault_urls:
            value = _kv.get_secret_value(token, vault_url, name)
            if value:
                found = value
                break
        if found is None:
            errors[name] = f"'{name}' 를 어떤 Key Vault에서도 찾지 못했습니다"
        else:
            values[name] = found
    return values, errors


def main():
    names = read_request()
    values, errors = resolve(names)
    sys.stdout.write(json.dumps({"protocolVersion": 1, "values": values, "errors": errors}))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# Status: production
# Path: systemd/user/devforge-sp-secret-check.service (weekly, `check`); admin runbook (`rotate`)
"""Azure SP client secret: monitor (validity/expiry) + Graph read/rotate.

Subcommands:
  check (default)  live Key Vault token probe + expiry alert from metadata.
                   Used by the weekly watchdog oneshot.
  read-expiry      read the credential expiry via Graph -> update metadata.
  rotate           add a new credential via Graph -> update the secret file + metadata.

[WHY] Graph read/rotate require the SP to hold Application.ReadWrite(.All/OwnedBy),
granted + admin-consented. Until then `check` still works via the live probe plus a
manually recorded `expires_at`. Secrets are never printed (written to files, 0600).
"""
import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

HOME = os.path.expanduser("~")
SECRET_FILE = os.environ.get(
    "AZURE_KEYVAULT_CLIENT_SECRET_FILE",
    os.path.join(HOME, ".config/devforge/azure-client-secret"),
)
META_FILE = os.environ.get(
    "AZURE_KEYVAULT_CLIENT_SECRET_META",
    os.path.join(HOME, ".config/devforge/azure-client-secret.meta.json"),
)
TENANT_ID = os.environ.get("AZURE_KEYVAULT_TENANT_ID", "9ec65251-a106-4dc3-9878-4278caa80b1b")
CLIENT_ID = os.environ.get("AZURE_KEYVAULT_CLIENT_ID", "fcf857e3-686e-49a8-b58c-f49a33e7b840")
APP_ID = os.environ.get("AZURE_SP_SECRET_APP_ID", CLIENT_ID)
WARN_DAYS = float(os.environ.get("AZURE_SP_SECRET_WARN_DAYS", "30"))
CRITICAL_DAYS = float(os.environ.get("AZURE_SP_SECRET_CRITICAL_DAYS", "7"))
VAULT_SCOPE = "https://vault.azure.net/.default"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOKEN_URL = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"


def parse_expiry(value: object) -> Optional[datetime.datetime]:
    """Parse an ISO-8601 date/datetime (trailing Z tolerated). None if absent/invalid."""
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.timezone.utc)


def load_expiry(meta_path: str) -> Optional[datetime.datetime]:
    """Read expires_at from the secret metadata JSON. None if missing/unreadable."""
    return parse_expiry(load_meta(meta_path).get("expires_at"))


def load_meta(meta_path: str) -> Dict[str, Any]:
    """Load the secret metadata JSON. {} if missing/unreadable."""
    try:
        with open(meta_path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_meta(meta_path: str, meta: Dict[str, Any]) -> None:
    """Persist the metadata JSON (0600)."""
    os.makedirs(os.path.dirname(meta_path), exist_ok=True)
    fd = os.open(meta_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(meta, f, indent=2, sort_keys=True)


def classify(
    expiry: Optional[datetime.datetime],
    now: datetime.datetime,
    invalid: bool,
    warn_days: float = WARN_DAYS,
    critical_days: float = CRITICAL_DAYS,
) -> Tuple[bool, str]:
    """Return (ok, message). Pure — unit-testable."""
    if invalid:
        return False, "credential invalid — token fetch failed"
    if expiry is None:
        return True, f"valid; expiry unknown (set expires_at in {META_FILE})"
    days = (expiry - now).total_seconds() / 86400
    if days <= critical_days:
        return False, f"expires in {days:.1f}d (critical <= {critical_days:.0f}d)"
    if days <= warn_days:
        return False, f"expires in {days:.1f}d (warn <= {warn_days:.0f}d)"
    return True, f"valid; expires in {days:.1f}d"


def fetch_token(
    scope: str, secret_file: str = SECRET_FILE, timeout: int = 30
) -> Optional[str]:
    """OAuth client-credentials token for `scope`. None on failure.

    The secret is sent on curl's stdin (never on argv, so it cannot leak via /proc).
    """
    try:
        with open(secret_file) as f:
            secret = f.read().strip()
    except OSError:
        return None
    if not secret:
        return None
    body = (
        "grant_type=client_credentials"
        f"&client_id={CLIENT_ID}&client_secret={secret}&scope={scope}"
    )
    result = subprocess.run(
        [
            "curl", "-s", "-X", "POST", TOKEN_URL,
            "-H", "Content-Type: application/x-www-form-urlencoded", "--data", "@-",
        ],
        input=body, capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout).get("access_token")
    except json.JSONDecodeError:
        return None


def probe_token(secret_file: str = SECRET_FILE) -> bool:
    """Live validity probe: can this credential fetch a Key Vault token?"""
    return fetch_token(VAULT_SCOPE, secret_file) is not None


def _graph(
    token: str, method: str, path: str, params: Optional[Dict[str, str]] = None,
    payload: Optional[Dict[str, Any]] = None, timeout: int = 30,
) -> Dict[str, Any]:
    """Call Microsoft Graph. Returns the parsed body (or {'error': ...})."""
    url = f"{GRAPH_BASE}/{path}"
    if method == "GET" and params:
        cmd = ["curl", "-s", "-G", "-H", f"Authorization: Bearer {token}", url]
        for key, value in params.items():
            cmd += ["--data-urlencode", f"{key}={value}"]
        stdin_data = None
    else:
        cmd = [
            "curl", "-s", "-X", method, "-H", f"Authorization: Bearer {token}",
            "-H", "Content-Type: application/json", url,
        ]
        if payload is not None:
            cmd += ["--data", "@-"]
        stdin_data = json.dumps(payload) if payload is not None else None
    result = subprocess.run(
        cmd, input=stdin_data, capture_output=True, text=True, timeout=timeout
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"error": {"code": "parse_error", "message": result.stdout[:200]}}


def min_credential_expiry(apps: List[Dict[str, Any]]) -> Optional[datetime.datetime]:
    """Earliest passwordCredential endDateTime across the returned applications. Pure."""
    ends: List[datetime.datetime] = []
    for app in apps:
        for cred in app.get("passwordCredentials") or []:
            parsed = parse_expiry(cred.get("endDateTime"))
            if parsed:
                ends.append(parsed)
    return min(ends) if ends else None


def build_rotation_payload(
    now: datetime.datetime, years: float = 1.0, display_name: Optional[str] = None
) -> Dict[str, Any]:
    """Graph addPassword payload. Pure — unit-testable."""
    end = now + datetime.timedelta(days=365 * years)
    return {
        "passwordCredential": {
            "displayName": display_name or f"rotated-{now.date().isoformat()}",
            "endDateTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    }


def _app_object_id(token: str) -> Tuple[Optional[str], str]:
    body = _graph(
        token, "GET", "applications",
        {"$filter": f"appId eq '{APP_ID}'", "$select": "id"},
    )
    if "error" in body:
        return None, str(body["error"].get("code", "graph_error"))
    values = body.get("value") or []
    if not values:
        return None, "application not found"
    return values[0]["id"], "ok"


def cmd_check(_args: argparse.Namespace) -> int:
    now = datetime.datetime.now(datetime.timezone.utc)
    expiry = load_expiry(META_FILE)
    invalid = not probe_token()
    ok, message = classify(expiry, now, invalid)
    print(f"{'OK' if ok else 'ALERT'}: {message}")
    return 0 if ok else 1


def cmd_read_expiry(_args: argparse.Namespace) -> int:
    token = fetch_token(GRAPH_SCOPE)
    if not token:
        print("ERROR: could not obtain Graph token", file=sys.stderr)
        return 2
    body = _graph(
        token, "GET", "applications",
        {"$filter": f"appId eq '{APP_ID}'", "$select": "id,passwordCredentials"},
    )
    if "error" in body:
        print(f"ERROR: Graph denied ({body['error'].get('code')}) — needs Application.Read",
              file=sys.stderr)
        return 2
    expiry = min_credential_expiry(body.get("value") or [])
    if expiry is None:
        print("ERROR: no passwordCredential visible", file=sys.stderr)
        return 2
    meta = load_meta(META_FILE)
    meta["expires_at"] = expiry.isoformat()
    meta["source"] = "graph"
    save_meta(META_FILE, meta)
    print(f"expires_at={expiry.isoformat()}")
    return 0


def cmd_rotate(args: argparse.Namespace) -> int:
    token = fetch_token(GRAPH_SCOPE)
    if not token:
        print("ERROR: could not obtain Graph token", file=sys.stderr)
        return 2
    object_id, detail = _app_object_id(token)
    if not object_id:
        print(f"ERROR: {detail} — needs Application.ReadWrite", file=sys.stderr)
        return 2
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = build_rotation_payload(now, years=args.years)
    body = _graph(token, "POST", f"applications/{object_id}/addPassword", payload=payload)
    if "error" in body or not body.get("secretText"):
        code = (body.get("error") or {}).get("code", "no_secretText")
        print(f"ERROR: rotation failed ({code}) — needs Application.ReadWrite", file=sys.stderr)
        return 2

    if os.path.exists(SECRET_FILE):
        backup = f"{SECRET_FILE}.bak.{now.strftime('%Y%m%dT%H%M%SZ')}"
        shutil.copy2(SECRET_FILE, backup)
        os.chmod(backup, 0o600)
    fd = os.open(SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(body["secretText"])

    meta = load_meta(META_FILE)
    meta.update({
        "expires_at": body.get("endDateTime"),
        "key_id": body.get("keyId"),
        "rotated_at": now.isoformat(),
        "source": "graph",
    })
    save_meta(META_FILE, meta)
    print(
        f"rotated: keyId={body.get('keyId')} expires={body.get('endDateTime')} "
        f"(secret -> {SECRET_FILE}); remove the old credential via Graph when verified"
    )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Azure SP client secret monitor/rotate")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("check", help="validity probe + expiry alert (default)")
    sub.add_parser("read-expiry", help="read credential expiry via Graph -> metadata")
    rotate = sub.add_parser("rotate", help="add a new credential via Graph")
    rotate.add_argument("--years", type=float, default=1.0, help="validity in years")
    args = parser.parse_args(argv)
    if args.cmd == "read-expiry":
        return cmd_read_expiry(args)
    if args.cmd == "rotate":
        return cmd_rotate(args)
    return cmd_check(args)


if __name__ == "__main__":
    sys.exit(main())

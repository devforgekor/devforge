#!/usr/bin/env python3
# Status: production
# Path: systemd/user/devforge-sp-secret-check.service (weekly oneshot)
"""Verify the Azure SP client secret: live validity probe + expiry alert.

[WHY] The SP cannot read or rotate its own credential (Insufficient privileges),
so expiry is tracked from a metadata file and validated by a live token fetch.
Exits 1 when the credential is invalid/expired or expires within the SLE window,
so the watchdog oneshot-result check raises an incident.
"""
import datetime
import json
import os
import subprocess
import sys
from typing import Optional, Tuple

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
CLIENT_ID = os.environ.get("AZURE_KEYVAULT_CLIENT_ID", "abc5aab0-5394-46e0-bf4d-daf4129d1d78")
WARN_DAYS = float(os.environ.get("AZURE_SP_SECRET_WARN_DAYS", "30"))
CRITICAL_DAYS = float(os.environ.get("AZURE_SP_SECRET_CRITICAL_DAYS", "7"))
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
    try:
        with open(meta_path) as f:
            return parse_expiry(json.load(f).get("expires_at"))
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


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


def probe_token(secret_file: str = SECRET_FILE, timeout: int = 30) -> bool:
    """Live validity probe: fetch a Key Vault token. True when the credential works.

    The secret is sent on curl's stdin (never on argv, so it cannot leak via /proc).
    """
    try:
        with open(secret_file) as f:
            secret = f.read().strip()
    except OSError:
        return False
    if not secret:
        return False
    body = (
        "grant_type=client_credentials"
        f"&client_id={CLIENT_ID}"
        f"&client_secret={secret}"
        "&scope=https://vault.azure.net/.default"
    )
    result = subprocess.run(
        [
            "curl",
            "-s",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            "-X",
            "POST",
            TOKEN_URL,
            "-H",
            "Content-Type: application/x-www-form-urlencoded",
            "--data",
            "@-",
        ],
        input=body,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.returncode == 0 and result.stdout.strip().startswith("2")


def main() -> int:
    now = datetime.datetime.now(datetime.timezone.utc)
    expiry = load_expiry(META_FILE)
    invalid = not probe_token()
    ok, message = classify(expiry, now, invalid)
    print(f"{'OK' if ok else 'ALERT'}: {message}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""Azure Blob uploader — single shared utility for all pipeline outputs.

Generates a self-contained review-bundle.md from any pipeline result
and uploads to Azure Blob with a 7-day SAS URL.

Usage:
  from lib.blob_uploader import upload_review_bundle

  url = upload_review_bundle(
      content=final_report_markdown,
      pipeline="debate",       # debate | code_mod | extract
      session_id="20260525T...",
  )
  print(f"Review URL: {url}")
"""

import os
import subprocess  # noqa: used by _shorten_with_droplr
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

from azure.storage.blob import BlobSasPermissions, BlobServiceClient, generate_blob_sas

ACCOUNT_NAME = "stshareddevforgeprodkrc"
CONTAINER = "devforge"

_account_key: Optional[str] = None


def _get_account_key() -> str:
    global _account_key
    if _account_key:
        return _account_key
    # Read from secrets file directly (avoids bash semicolon issues)
    secrets_path = os.path.expanduser("~/.config/devforge/secrets.env")
    with open(secrets_path) as f:
        for line in f:
            if line.startswith("AZURE_STORAGE_ACCOUNT_KEY="):
                _account_key = line.strip().split("=", 1)[1].strip("'\"")
                break
    if not _account_key:
        raise RuntimeError("AZURE_STORAGE_ACCOUNT_KEY not found in secrets.env")
    return _account_key


def _upload_blob(blob_name: str, content: Union[str, bytes]) -> str:
    """Upload content to Azure Blob, return SAS URL with 7-day read permission."""
    account_key = _get_account_key()
    account_url = f"https://{ACCOUNT_NAME}.blob.core.windows.net"

    service = BlobServiceClient(account_url=account_url, credential=account_key)
    blob_client = service.get_blob_client(container=CONTAINER, blob=blob_name)
    if isinstance(content, str):
        content = content.encode("utf-8")
    blob_client.upload_blob(content, overwrite=True)

    sas_token = generate_blob_sas(
        account_name=ACCOUNT_NAME,
        container_name=CONTAINER,
        blob_name=blob_name,
        account_key=account_key,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.now(timezone.utc) + timedelta(days=7),
    )
    return f"https://{ACCOUNT_NAME}.blob.core.windows.net/{CONTAINER}/{blob_name}?{sas_token}"


def _shorten_with_droplr(long_url: str) -> Optional[str]:
    """Shorten a URL using drplr CLI (Droplr link shortening).

    Requires drplr to be installed and authenticated (npm install -g drplr).
    Reads DRPLR_EMAIL/DRPLR_PASSWORD from secrets.env if not yet authed.
    """
    try:
        # Check if drplr is available
        subprocess.run(["drplr", "--help"], capture_output=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    try:
        result = subprocess.run(
            ["drplr", "link", long_url, "--porcelain"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            short_url = result.stdout.strip()
            if short_url:
                return short_url

        # If auth failed, try to login from secrets
        if "auth" in (result.stderr or "").lower() or "login" in (result.stderr or "").lower():
            secrets_path = os.path.expanduser("~/.config/devforge/secrets.env")
            email = password = None
            with open(secrets_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("DRPLR_EMAIL="):
                        email = line.split("=", 1)[1].strip("'\"")
                    elif line.startswith("DRPLR_PASSWORD="):
                        password = line.split("=", 1)[1].strip("'\"")
            if email and password:
                subprocess.run(
                    ["drplr", "auth", "login", email, password],
                    capture_output=True,
                    timeout=15,
                )
                # Retry
                result = subprocess.run(
                    ["drplr", "link", long_url, "--porcelain"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode == 0:
                    short_url = result.stdout.strip()
                    if short_url:
                        return short_url
        return None
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════════


def upload_review_bundle(
    content: str,
    pipeline: str,
    session_id: str,
    metadata: Optional[dict] = None,
    shortlink: bool = False,
) -> str:
    """Wrap pipeline output as a review-ready markdown bundle and upload.

    Args:
        content: The final report / output in markdown format
        pipeline: "debate", "code_mod", or "extract"
        session_id: Unique session identifier
        metadata: Optional dict with keys like question, model, confidence, etc.
        shortlink: If True, also shorten the SAS URL with Droplr.

    Returns:
        SAS URL string (7-day expiry) for downloading the review bundle.
        If shortlink=True and Droplr shortening succeeds, returns the d.pr URL.
    """
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    blob_name = f"{pipeline}/{date_str}/{session_id}/review-bundle.md"

    # Build self-contained review bundle
    bundle_lines = [
        f"# DevForge {pipeline.upper()} — Review Bundle",
        "",
        f"**Session:** `{session_id}`",
        f"**Generated:** {now.isoformat()}",
        f"**Pipeline:** {pipeline}",
    ]

    if metadata:
        for key, val in metadata.items():
            bundle_lines.append(f"**{key}:** {val}")

    bundle_lines.extend(
        [
            "",
            "---",
            "",
            content,
            "",
            "---",
            "",
            "*End of review bundle. Submit this entire document to any AI for review.*",
        ]
    )

    bundle = "\n".join(bundle_lines)
    sas_url = _upload_blob(blob_name, bundle)

    if shortlink:
        short = _shorten_with_droplr(sas_url)
        if short:
            return short

    return sas_url


def upload_raw(
    content: str, pipeline: str, session_id: str, filename: str, shortlink: bool = False
) -> str:
    """Upload raw file (JSON, YAML, etc.) to the session directory.

    Args:
        shortlink: If True, also shorten the SAS URL with Droplr.

    Returns:
        SAS URL string, or d.pr short URL if shortlink=True and successful.
    """
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    blob_name = f"{pipeline}/{date_str}/{session_id}/{filename}"
    sas_url = _upload_blob(blob_name, content)

    if shortlink:
        short = _shorten_with_droplr(sas_url)
        if short:
            return short

    return sas_url

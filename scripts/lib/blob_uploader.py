#!/usr/bin/env python3.11
# Status: production
# Path: imported by — cli.py, lib/debate/local_debate.py, droplr_upload.py
"""Review-bundle uploader — OCI Object Storage + optional Droplr short link.

Stage pipeline outputs (debate / code_mod / extract review bundles) into the
OCI bucket under releases/, and return a time-limited PAR download URL
(optionally shortened via Droplr).

Usage:
  from lib.blob_uploader import upload_review_bundle

  url = upload_review_bundle(content=final_report_markdown,
                             pipeline="debate",
                             session_id="20260525T...")
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional, Union

from lib.droplr import shorten
from lib.oci_storage import create_par, put_object

PAR_HOURS = int(os.environ.get("OCI_PAR_HOURS", "168"))  # 168h = 7 days (configurable)


def _upload_blob(blob_name: str, content: Union[str, bytes]) -> str:
    """Upload content to OCI Object Storage, return a PAR download URL."""
    put_object(blob_name, content)
    return create_par(blob_name, access_type="ObjectRead", hours=PAR_HOURS)


def _shorten_with_droplr(long_url: str) -> Optional[str]:
    """Shorten a URL with Droplr (HTTP API). Returns None on failure."""
    return shorten(long_url)


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
    """Wrap pipeline output as a review-ready markdown bundle and upload to OCI.

    Args:
        content: final report/output in markdown
        pipeline: "debate", "code_mod", or "extract"
        session_id: unique session identifier
        metadata: optional dict (question, model, confidence, ...)
        shortlink: if True, also shorten the PAR URL with Droplr

    Returns:
        PAR URL (default 7-day expiry), or a d.pr short URL if shortlink succeeds.
    """
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    blob_name = f"releases/{pipeline}/{date_str}/{session_id}/review-bundle.md"

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
    bundle_lines.extend(["", "---", "", content, "", "---", "",
                         "*End of review bundle. Submit this entire document to any AI for review.*"])

    url = _upload_blob(blob_name, "\n".join(bundle_lines))
    if shortlink:
        short = _shorten_with_droplr(url)
        if short:
            return short
    return url


def upload_raw(
    content: str, pipeline: str, session_id: str, filename: str, shortlink: bool = False
) -> str:
    """Upload a raw file (JSON, YAML, ...) to the session directory in OCI.

    Returns:
        PAR URL, or a d.pr short URL if shortlink=True and shortening succeeds.
    """
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    blob_name = f"releases/{pipeline}/{date_str}/{session_id}/{filename}"
    url = _upload_blob(blob_name, content)
    if shortlink:
        short = _shorten_with_droplr(url)
        if short:
            return short
    return url

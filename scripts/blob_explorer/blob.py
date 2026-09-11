#!/usr/bin/env python3
# Status: production
# Path: imported by — blob_explorer/handler.py
"""OCI Object Storage backend for the file-exchange UI (send + receive).

Replaces the former Azure Blob backend. All names handled here are RELATIVE to
OCI_EXCHANGE_ROOT (default "uploads/"), e.g. "documents/20260911_x.md".

Exports (interface consumed by handler.py):
  _list_blobs, _virtual_tree, _generate_sas, _share_url, _upload_blob, SAS_HOURS
"""

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Union

from lib.oci_storage import create_par, list_objects, put_object

ROOT = os.environ.get("OCI_EXCHANGE_ROOT", "uploads").strip("/")
SAS_HOURS = int(os.environ.get("BLOB_EXPLORER_PAR_HOURS", os.environ.get("BLOB_EXPLORER_SAS_HOURS", "1")))
SHORTEN = os.environ.get("BLOB_EXPLORER_SHORTEN", "1") == "1"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".bmp"}


def _physical(rel: str) -> str:
    rel = rel.lstrip("/")
    return f"{ROOT}/{rel}" if rel else f"{ROOT}/"


def _rel(full: str) -> str:
    prefix = f"{ROOT}/"
    return full[len(prefix):] if full.startswith(prefix) else full


def _list_blobs(prefix: str) -> list[dict]:
    """List objects under a path relative to ROOT, returning ROOT-relative names."""
    results = []
    for o in list_objects(_physical(prefix)):
        name = o["name"]
        if name.endswith("/"):  # folder placeholder marker
            continue
        if name == f"{ROOT}/":
            continue
        results.append({"name": _rel(name), "size": o["size"], "updated": o.get("updated", "")})
    results.sort(key=lambda b: (0 if "/" in b["name"][len(prefix):] else 1, b["name"]))
    return results


def _virtual_tree(prefix: str, blobs: list[dict]) -> dict:
    dirs: dict[str, dict] = {}
    files = []
    strip = len(prefix)
    for b in blobs:
        rel = b["name"][strip:]
        if "/" in rel:
            dname = rel.split("/")[0]
            if dname:
                dirs.setdefault(dname, {"name": dname, "count": 0, "total_size": 0})
                dirs[dname]["count"] += 1
                dirs[dname]["total_size"] += b["size"]
        else:
            files.append({"name": rel, "size": b["size"], "updated": b.get("updated", "")})
    return {"dirs": sorted(dirs.values(), key=lambda d: d["name"]), "files": files}


def _generate_sas(rel_name: str) -> str:
    """Return a time-limited OCI PAR download URL for a ROOT-relative object."""
    return create_par(_physical(rel_name), access_type="ObjectRead", hours=SAS_HOURS)


def _share_url(rel_name: str) -> str:
    """Download PAR, optionally shortened via Droplr for the final share link."""
    url = _generate_sas(rel_name)
    if not SHORTEN:
        return url
    try:
        from lib.droplr import shorten  # HTTP Basic, no Node CLI needed

        return shorten(url) or url
    except Exception:
        return url


def _upload_blob(filename: str, data: Union[str, bytes]) -> str:
    """Upload to uploads/{images|documents}/ and return the ROOT-relative name."""
    utc_ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    sub = "images" if Path(filename).suffix.lower() in IMAGE_EXTS else "documents"
    rel_name = f"{sub}/{utc_ts}_{filename}"
    put_object(_physical(rel_name), data)
    return rel_name


def presign_upload(filename: str) -> dict:
    """Create a write-PAR so a client can PUT directly to OCI (bypassing the server).

    Returns {object_name, upload_url, download_url}. Browser use additionally
    requires bucket CORS (not configurable with OCI SDK 2.180/CLI 3.88); scripts
    and CLI clients can PUT without CORS.
    """
    utc_ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    sub = "images" if Path(filename).suffix.lower() in IMAGE_EXTS else "documents"
    rel_name = f"{sub}/{utc_ts}_{filename}"
    upload_url = create_par(_physical(rel_name), access_type="ObjectWrite", hours=SAS_HOURS)
    return {
        "object_name": rel_name,
        "upload_url": upload_url,
        "download_url": _share_url(rel_name),
    }

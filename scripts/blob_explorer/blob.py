#!/usr/bin/env python3
# Status: production
"""Azure Blob storage operations."""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions

ACCOUNT_NAME = "stshareddevforgeprodkrc"
CONTAINER = "devforge"
SAS_HOURS = int(os.environ.get("BLOB_EXPLORER_SAS_HOURS", "1"))
UPLOAD_PREFIX = "uploads/"

_account_key: Optional[str] = None


def _get_account_key() -> str:
    global _account_key
    if _account_key:
        return _account_key
    sf = Path.home() / ".config/devforge/secrets.env"
    with open(sf) as f:
        for line in f:
            if line.startswith("AZURE_STORAGE_ACCOUNT_KEY="):
                _account_key = line.strip().split("=", 1)[1].strip("'\"")
                break
    if not _account_key:
        raise RuntimeError("AZURE_STORAGE_ACCOUNT_KEY not found")
    return _account_key


def _blob_service():
    return BlobServiceClient(
        account_url=f"https://{ACCOUNT_NAME}.blob.core.windows.net",
        credential=_get_account_key(),
    )


def _list_blobs(prefix: str) -> list[dict]:
    svc = _blob_service()
    cc = svc.get_container_client(CONTAINER)
    results = []
    for blob in cc.list_blobs(name_starts_with=prefix):
        if blob.name == prefix:
            continue
        results.append({
            "name": blob.name,
            "size": blob.size or 0,
            "updated": blob.last_modified.isoformat() if blob.last_modified else "",
        })
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


def _generate_sas(blob_name: str) -> str:
    key = _get_account_key()
    sas = generate_blob_sas(
        account_name=ACCOUNT_NAME, container_name=CONTAINER, blob_name=blob_name,
        account_key=key, permission=BlobSasPermissions(read=True),
        expiry=datetime.now(timezone.utc) + timedelta(hours=SAS_HOURS),
    )
    return f"https://{ACCOUNT_NAME}.blob.core.windows.net/{CONTAINER}/{blob_name}?{sas}"


def _upload_blob(filename: str, data: bytes) -> str:
    svc = _blob_service()
    cc = svc.get_container_client(CONTAINER)
    utc_ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    blob_name = f"{UPLOAD_PREFIX}{utc_ts}_{filename}"
    cc.upload_blob(blob_name, data, overwrite=True)
    return blob_name

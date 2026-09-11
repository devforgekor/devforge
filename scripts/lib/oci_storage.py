#!/usr/bin/env python3.11
# Status: production
# Path: imported by — blob_explorer/blob.py, lib
"""OCI Object Storage helper — list / put / delete / PAR for DevForge.

Auth: ~/.oci/config (DEFAULT profile). In containers, mount ~/.oci read-only.
Bucket: $OCI_BUCKET (default devforge-standard).

Usage:
  from lib.oci_storage import list_objects, put_object, create_par
  objs = list_objects("uploads/")
  put_object("uploads/documents/x.md", b"...", content_type="text/markdown")
  url = create_par("uploads/documents/x.md", access_type="ObjectRead", hours=1)
"""

from __future__ import annotations

import mimetypes
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

DEFAULT_BUCKET = os.environ.get("OCI_BUCKET", "devforge-standard")
CONFIG_FILE = os.environ.get("OCI_CONFIG_FILE", "~/.oci/config")
PROFILE = os.environ.get("OCI_PROFILE", "DEFAULT")

_client = None
_namespace: Optional[str] = None


def _client_and_ns():
    global _client, _namespace
    if _client is None:
        import oci

        cfg = oci.config.from_file(os.path.expanduser(CONFIG_FILE), PROFILE)
        _client = oci.object_storage.ObjectStorageClient(cfg)
        _namespace = _client.get_namespace().data
    return _client, _namespace


def endpoint() -> str:
    c, _ = _client_and_ns()
    # base_client.endpoint may be a template e.g.
    # https://objectstorage.<region>.{dualStack?ds.oci.:}oraclecloud.com
    ep = re.sub(r"\{[^}]*\}", "", c.base_client.endpoint.rstrip("/"))
    return ep


def list_objects(prefix: str, bucket: str = DEFAULT_BUCKET) -> list[dict]:
    """List objects under prefix. Returns [{name, size, updated}]."""
    c, ns = _client_and_ns()
    out: list[dict] = []
    start = None
    while True:
        resp = c.list_objects(ns, bucket, prefix=prefix, start=start, limit=1000)
        data = resp.data
        for o in data.objects:
            out.append(
                {
                    "name": o.name,
                    "size": o.size or 0,
                    "updated": o.time_created.isoformat() if o.time_created else "",
                }
            )
        start = getattr(data, "next_start_with", None)
        if not start:
            break
    return out


def put_object(
    name: str, data: Union[bytes, str], content_type: Optional[str] = None, bucket: str = DEFAULT_BUCKET
) -> None:
    c, ns = _client_and_ns()
    if isinstance(data, str):
        data = data.encode("utf-8")
    if not content_type:
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    c.put_object(ns, bucket, name, data, content_type=content_type)


def delete_object(name: str, bucket: str = DEFAULT_BUCKET) -> None:
    c, ns = _client_and_ns()
    c.delete_object(ns, bucket, name)


def create_par(
    object_name: str,
    access_type: str = "ObjectRead",
    hours: float = 1.0,
    bucket: str = DEFAULT_BUCKET,
    name: Optional[str] = None,
) -> str:
    """Create a Pre-Authenticated Request and return the full URL.

    access_type: ObjectRead | ObjectWrite | ObjectReadWrite
                 | AnyObjectRead | AnyObjectWrite | AnyObjectReadWrite
    """
    import oci

    c, ns = _client_and_ns()
    details = oci.object_storage.models.CreatePreauthenticatedRequestDetails(
        name=name or f"df-{uuid.uuid4().hex[:12]}",
        access_type=access_type,
        object_name=object_name,
        time_expires=datetime.now(timezone.utc) + timedelta(hours=hours),
    )
    par = c.create_preauthenticated_request(ns, bucket, details).data
    return f"{endpoint()}{par.access_uri}"

#!/usr/bin/env python3
"""oci/config | OCI auth loader: env vars or ~/.oci/config key file, base64 key content support | load_oci_config()"""

from __future__ import annotations

import os
import base64
from pathlib import Path


def load_oci_config(key_env_var: str = "OCI_KEY_PATH", default_key: str = "~/.oci/oci_api_key.pem") -> dict[str, str]:
    config = {
        "user": os.environ.get("OCI_USER_OCID"),
        "tenancy": os.environ.get("OCI_TENANCY_OCID"),
        "region": os.environ.get("OCI_REGION", "ap-chuncheon-1"),
        "fingerprint": os.environ.get("OCI_FINGERPRINT"),
    }

    key_path = os.environ.get(key_env_var)
    if not key_path:
        key_path = str(Path(default_key).expanduser())

    key_content = os.environ.get("OCI_KEY_CONTENT")
    if key_content:
        key_path = "/tmp/oci_api_key.pem"
        with open(key_path, "w") as f:
            f.write(base64.b64decode(key_content).decode())
        os.chmod(key_path, 0o600)

    config["key_file"] = key_path

    missing = [k for k, v in config.items() if not v]
    if missing:
        raise ValueError(f"Missing OCI config keys: {missing}")

    return config

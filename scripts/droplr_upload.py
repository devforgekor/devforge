#!/usr/bin/env python3.11
# Status: production
# Path: bash alias: droplr, dplr
"""Upload a file to Azure Blob, shorten with Droplr, print d.pr URL.

Usage:
  droplr file.pdf
  droplr file.pdf --private
  droplr file.pdf --title "Report"
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add project scripts to path
sys.path.insert(0, "/opt/projects/server/scripts")
from lib.blob_uploader import _shorten_with_droplr, _upload_blob  # noqa: E402


def upload_file(filepath: str, title: str = "") -> str:
    path = Path(filepath)
    if not path.exists():
        print(f"File not found: {filepath}", file=sys.stderr)
        sys.exit(1)

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    timestamp = now.strftime("%H%M%S")
    blob_name = f"uploads/{date_str}/{timestamp}_{path.name}"

    content = path.read_bytes()
    sas_url = _upload_blob(blob_name, content)

    # Shorten with Droplr
    short = _shorten_with_droplr(sas_url)
    if short:
        return short

    # Fallback: raw SAS URL
    return sas_url


def main():
    parser = argparse.ArgumentParser(description="Upload file to Blob + Droplr short link")
    parser.add_argument("file", help="File to upload")
    parser.add_argument("--title", "-t", default="", help="Optional title")
    args = parser.parse_args()

    url = upload_file(args.file, args.title)
    print(url)


if __name__ == "__main__":
    main()

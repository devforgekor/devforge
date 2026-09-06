#!/usr/bin/env python3.11
# Status: production
# Path: bash alias: droplr, dplr
"""Upload a file to Azure Blob, shorten with Droplr, print d.pr URL.

Usage:
  droplr file.pdf
  droplr file.pdf --title "Report"
  droplr file.pdf --notion       # Also post to Notion
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add project scripts to path
sys.path.insert(0, "/opt/projects/server/scripts")
from lib.blob_uploader import _shorten_with_droplr, _upload_blob  # noqa: E402


def upload_file(filepath: str, title: str = "", notion: bool = False) -> str:
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
    final_url = short or sas_url

    # Post to Notion if requested
    if notion:
        try:
            from lib.notion_client import append_memo_with_blob  # noqa: E402
            memo_title = title or path.name
            memo_url = append_memo_with_blob(
                markdown=f"**{memo_title}** uploaded to DevForge",
                blob_url=final_url,
                title=memo_title,
                filename=path.name,
            )
            # Print both URLs
            print(final_url)
            print(f"Notion: {memo_url}", file=sys.stderr)
        except Exception as e:
            print(f"Notion post failed: {e}", file=sys.stderr)
            print(final_url)
    else:
        print(final_url)

    return final_url


def main():
    parser = argparse.ArgumentParser(description="Upload file to Blob + Droplr short link")
    parser.add_argument("file", help="File to upload")
    parser.add_argument("--title", "-t", default="", help="Optional title")
    parser.add_argument("--notion", "-n", action="store_true", help="Also post to Notion")
    args = parser.parse_args()

    upload_file(args.file, args.title, notion=args.notion)


if __name__ == "__main__":
    main()

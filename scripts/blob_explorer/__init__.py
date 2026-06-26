#!/usr/bin/env python3
# Status: production
"""Azure Blob web interface: send + receive — split from blob_explorer.py."""

import os
from http.server import ThreadingHTTPServer

from blob_explorer.handler import BlobHandler

LISTEN_ADDR = os.environ.get("BLOB_EXPLORER_LISTEN", "127.0.0.1:8085")


def main():
    host, port_text = LISTEN_ADDR.rsplit(":", 1)
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((host, int(port_text)), BlobHandler)
    print(f"[blob] listening on {host}:{port_text}", file=__import__("sys").stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass

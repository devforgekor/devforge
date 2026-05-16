import urllib.request
import sys

try:
    resp = urllib.request.urlopen("http://localhost:8000/health")
    if resp.status == 200:
        sys.exit(0)
except Exception:
    pass
sys.exit(1)

#!/bin/bash
exec python3 -m uvicorn devforge_fastapi.app:app --host 0.0.0.0 --port 8002 --log-level info

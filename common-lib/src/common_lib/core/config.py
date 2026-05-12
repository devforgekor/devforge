#!/usr/bin/env python3
"""core/config | load .env files into os.environ, auto-detect papertrail root | load_config()"""
import os
import sys
from pathlib import Path
from typing import Dict, Optional

def load_config(repo_root: Optional[str] = None) -> None:
    if repo_root is None:
        # 현재 스크립트 위치에서 papertrail/ 디렉토리를 찾음
        current = Path(__file__).resolve()
        while current.name != "papertrail" and current.parent != current:
            current = current.parent
        if current.name != "papertrail":
            current = Path.cwd()
        repo_root = str(current)
    env_file = Path(repo_root) / ".env"
    if env_file.is_file():
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    key, _, value = line.partition("=")
                    if key:
                        os.environ.setdefault(key.strip(), value.strip())
        print(f"[INFO] .env 파일 로드됨: {env_file}", file=sys.stderr)
    else:
        print(f"[WARN] .env 파일이 없습니다: {env_file}", file=sys.stderr)

# 기본 설정 상수 (환경 변수에서 로드되거나 기본값 사용)
LOCATION = os.getenv("LOCATION", "koreacentral")
REGION_CODE = os.getenv("REGION_CODE", "krc")
TEMPLATE_FILE = os.getenv("TEMPLATE_FILE", "infra/main.subscription.bicep")

def main():
    import argparse
    import sys
    parser = argparse.ArgumentParser(description="환경 변수 및 설정 관리")
    subparsers = parser.add_subparsers(dest="command", help="명령")

    # load_config
    subparsers.add_parser("load_config", help=".env 파일 로드")

    args = parser.parse_args()

    if args.command == "load_config":
        load_config()
        sys.exit(0)
    else:
        # 기본 테스트 실행
        print("=== core/config.py 테스트 ===")
        print(f"LOCATION: {LOCATION}")
        print(f"REGION_CODE: {REGION_CODE}")
        print(f"TEMPLATE_FILE: {TEMPLATE_FILE}")
        load_config()
        print("테스트 완료.")

if __name__ == "__main__":
    main()
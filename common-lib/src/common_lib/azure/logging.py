#!/usr/bin/env python3
"""azure/logging | JSON file logging: init per domain/module, write structured logs | init_json_logging(),write_json_log(),log_info(),log_error()"""

import os
import sys
import json
import datetime
from typing import Optional, Dict, Any

# 로그 디렉토리
LOG_DIR = os.getenv("LOG_DIR", "logs")
# 시스템 식별자 (프로젝트 이름)
SYSTEM = os.getenv("SYSTEM", "papertrail")
# Plane (collector, analyzer, storage)
PLANE = os.getenv("PLANE", "infrastructure")

def init_json_logging(domain: str, module: str) -> str:
    """
    JSON 로깅을 초기화하고 로그 파일 경로를 반환합니다.
    
    매개변수:
        domain: 도메인 (예: "azure", "collector")
        module: 모듈 이름 (예: "deploy", "monitor")
    
    반환값:
        로그 파일 경로
    """
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]  # 마이크로초 3자리
    log_file = os.path.join(LOG_DIR, SYSTEM, PLANE, f"{domain}_{module}_{timestamp}.jsonl")
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    return log_file

def write_json_log(log_file: str, level: str, message: str, extra: Optional[Dict[str, Any]] = None):
    """
    JSON 로그를 작성합니다.
    
    매개변수:
        log_file: 로그 파일 경로
        level: 로그 수준 (INFO, ERROR, WARN, DEBUG)
        message: 로그 메시지
        extra: 추가 필드 (JSON 객체)
    """
    timestamp = datetime.datetime.utcnow().isoformat(timespec="milliseconds") + "Z"
    entry = {
        "timestamp": timestamp,
        "level": level,
        "message": message,
        "extra": extra or {}
    }
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    # stdout에도 출력 (선택)
    print(json.dumps(entry, ensure_ascii=False))

def log_info(log_file: str, message: str):
    """
    INFO 로그를 작성합니다.
    """
    write_json_log(log_file, "INFO", message)

def log_error(log_file: str, message: str, extra: Optional[Dict[str, Any]] = None):
    """
    ERROR 로그를 작성합니다.
    """
    write_json_log(log_file, "ERROR", message, extra)

if __name__ == "__main__":
    # 독립 실행 테스트 블록
    print("=== azure/logging.py 테스트 ===", file=sys.stderr)
    # 임시 로그 파일 생성
    test_log = init_json_logging("test", "logging")
    print(f"로그 파일: {test_log}", file=sys.stderr)
    log_info(test_log, "테스트 로그 메시지")
    log_error(test_log, "테스트 에러 메시지", {"user": "test"})
    print("로그 내용:", file=sys.stderr)
    with open(test_log, "r", encoding="utf-8") as f:
        print(f.read(), end="")
    # 정리
    os.remove(test_log)
    try:
        os.removedirs(os.path.dirname(test_log))
    except OSError:
        pass
    print("테스트 완료.", file=sys.stderr)
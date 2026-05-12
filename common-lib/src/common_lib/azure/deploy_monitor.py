#!/usr/bin/env python3
"""azure/deploy_monitor | deployment monitoring: spinner polling, log streaming | monitor_deployment_with_spinner(),stream_deployment_logs()"""

import subprocess
import sys
import time
from typing import Optional

def monitor_deployment_with_spinner(deploy_name: str, timeout_seconds: int = 3600) -> bool:
    """
    배포 상태를 스피너와 함께 모니터링합니다.
    
    매개변수:
        deploy_name: 배포 이름
        timeout_seconds: 타임아웃(초), 기본값 3600초
    
    반환값:
        성공 시 True, 실패 시 False
    """
    start_time = time.time()
    spin_chars = "|/-\\"
    spin_idx = 0
    
    print(f"[INFO] 배포 모니터링 시작: {deploy_name} (타임아웃: {timeout_seconds}초)", file=sys.stderr)
    
    while True:
        try:
            result = subprocess.run(
                ["az", "deployment", "sub", "show", "--name", deploy_name,
                 "--query", "properties.provisioningState", "-o", "tsv"],
                capture_output=True,
                text=True,
                encoding="utf-8"
            )
            state = result.stdout.strip() if result.stdout else "Unknown"
        except subprocess.CalledProcessError:
            state = "Unknown"
        
        elapsed = int(time.time() - start_time)
        
        # 스피너 회전
        spin_char = spin_chars[spin_idx % len(spin_chars)]
        sys.stderr.write(f"\r[{spin_char}] 배포 상태: {state:<12} 경과: {elapsed:4}d")
        sys.stderr.flush()
        spin_idx += 1
        
        if state == "Succeeded":
            print(f"\n[SUCCESS] 배포 성공: {deploy_name} (소요 시간: {elapsed}초)", file=sys.stderr)
            return True
        elif state in ("Failed", "Canceled"):
            print(f"\n[ERROR] 배포 실패: {deploy_name} (상태: {state})", file=sys.stderr)
            # 오류 상세 출력
            subprocess.run(
                ["az", "deployment", "sub", "show", "--name", deploy_name,
                 "--query", "properties.error", "-o", "yaml"],
                stderr=subprocess.STDOUT
            )
            return False
        elif state in ("Running", "Accepted", "Creating", "Updating"):
            pass  # 계속 대기
        else:
            print(f"\n[WARN] 알 수 없는 상태: {state}", file=sys.stderr)
        
        # 타임아웃 체크
        if elapsed >= timeout_seconds:
            print(f"\n[ERROR] 배포 모니터링 시간 초과 ({timeout_seconds}초)", file=sys.stderr)
            return False
        
        time.sleep(5)

def stream_deployment_logs(deploy_name: str, limit: int = 20):
    """
    배포 로그를 스트리밍합니다 (선택적).
    
    매개변수:
        deploy_name: 배포 이름
        limit: 출력할 로그 줄 수 (기본값 20)
    """
    print(f"[INFO] 배포 로그 스트리밍 시작: {deploy_name}", file=sys.stderr)
    try:
        result = subprocess.run(
            ["az", "deployment", "operation", "list", "--name", deploy_name,
             "--query", "[].properties.statusMessage", "-o", "tsv"],
            capture_output=True,
            text=True,
            encoding="utf-8"
        )
        lines = result.stdout.strip().split("\n")[:limit]
        for line in lines:
            if line:
                print(line)
    except subprocess.CalledProcessError as e:
        print(f"[WARN] 로그 스트리밍 실패: {e.stderr}", file=sys.stderr)

if __name__ == "__main__":
    # 독립 실행 테스트 블록
    print("=== azure/deploy_monitor.py 테스트 ===", file=sys.stderr)
    print("이 모듈은 실제 Azure 배포를 모니터링하므로 테스트를 스킵합니다.", file=sys.stderr)
    print("함수 정의 확인:", file=sys.stderr)
    print(f"  monitor_deployment_with_spinner: {monitor_deployment_with_spinner.__doc__.splitlines()[0]}")
    print(f"  stream_deployment_logs: {stream_deployment_logs.__doc__.splitlines()[0]}")
    print("테스트 완료.", file=sys.stderr)
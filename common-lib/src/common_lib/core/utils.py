#!/usr/bin/env python3
"""core/utils | log to stderr, check az CLI, run shell commands | log_info(),log_warn(),log_error(),is_azure_cli_installed(),ensure_tool(),run_command()"""
import subprocess
import sys
import os

def log_info(message: str) -> None:
    """정보 로그 출력"""
    print(f"[INFO] {message}", file=sys.stderr)

def log_warn(message: str) -> None:
    """경고 로그 출력"""
    print(f"[WARN] {message}", file=sys.stderr)

def log_error(message: str) -> None:
    """오류 로그 출력"""
    print(f"[ERROR] {message}", file=sys.stderr)

def is_azure_cli_installed() -> bool:
    """Azure CLI가 설치되어 있는지 확인"""
    try:
        subprocess.run(["az", "--version"], capture_output=True, check=False)
        return True
    except (subprocess.SubprocessError, FileNotFoundError):
        return False

def ensure_tool(tool_name: str) -> bool:
    """
    필수 도구가 설치되었는지 확인합니다.

    매개변수:
        tool_name: 도구명 (예: "az", "docker")

    반환값:
        설치되어 있으면 True, 그렇지 않으면 False
    """
    try:
        subprocess.run([tool_name, "--version"], capture_output=True, check=False)
        return True
    except (subprocess.SubprocessError, FileNotFoundError):
        log_error(f"필수 도구 '{tool_name}'이(가) 설치되지 않았습니다.")
        return False

def run_command(cmd: str, cwd: str = None) -> subprocess.CompletedProcess:
    """
    쉘 명령을 실행하고 결과를 반환합니다.

    매개변수:
        cmd: 실행할 명령 문자열
        cwd: 작업 디렉토리 (None이면 현재 디렉토리)

    반환값:
        subprocess.CompletedProcess 객체
    """
    log_info(f"명령 실행: {cmd}")
    result = subprocess.run(
        cmd,
        shell=True,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        log_error(f"명령 실패: {cmd}\nstderr: {result.stderr}")
    return result

def main():
    """독립 실행 테스트"""
    print("=== core/utils.py 테스트 ===")
    log_info("정보 메시지입니다.")
    log_warn("경고 메시지입니다.")
    log_error("오류 메시지입니다.")
    if is_azure_cli_installed():
        print("✓ Azure CLI가 설치되어 있습니다.")
    else:
        print("✗ Azure CLI가 설치되어 있지 않습니다.")
    if ensure_tool("python3"):
        print("✓ python3가 설치되어 있습니다.")
    else:
        print("✗ python3가 설치되어 있지 않습니다.")
    # 간단한 명령 실행 테스트
    result = run_command("echo hello")
    print(f"명령 출력: {result.stdout.strip()}")
    print("테스트 완료.")

if __name__ == "__main__":
    main()
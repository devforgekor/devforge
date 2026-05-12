#!/usr/bin/env python3
"""azure/utils | az CLI helpers: ensure login, wait RG deletion, get user objectId, ensure extension | ensure_azure_login(),wait_for_rg_deletion(),get_current_user_object_id(),ensure_extension()"""

import subprocess
import sys
import time

def ensure_azure_login():
    """
    Azure CLI에 로그인되어 있는지 확인합니다.
    로그인되어 있지 않으면 오류 메시지를 출력하고 종료합니다.
    """
    try:
        subprocess.run(
            ["az", "account", "show"],
            capture_output=True,
            check=True,
            text=True,
            encoding="utf-8"
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("[ERROR] Azure CLI에 로그인되어 있지 않습니다. 'az login'을 실행하세요.", file=sys.stderr)
        sys.exit(1)

def wait_for_rg_deletion(rg: str, timeout: int = 1800):
    """
    리소스 그룹 삭제가 완료될 때까지 대기합니다.

    매개변수:
        rg: 리소스 그룹 이름
        timeout: 최대 대기 시간(초), 기본값 1800(30분)
    """
    print(f"[INFO] 리소스 그룹 '{rg}' 삭제 대기 중...", file=sys.stderr)
    try:
        subprocess.run(
            ["az", "group", "wait", "--deleted", "--name", rg, "--timeout", str(timeout)],
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8"
        )
    except subprocess.CalledProcessError:
        print("[WARN] 삭제 확인 시간 초과, 계속 진행합니다.", file=sys.stderr)

def get_current_user_object_id() -> str:
    """
    현재 로그인된 사용자의 Azure AD Object ID를 반환합니다.

    반환값:
        Object ID 문자열 (가져오지 못하면 빈 문자열)
    """
    try:
        result = subprocess.run(
            ["az", "ad", "signed-in-user", "show", "--query", "id", "-o", "tsv"],
            capture_output=True,
            check=True,
            text=True,
            encoding="utf-8"
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""

def ensure_extension(ext: str):
    """
    지정된 Azure CLI 확장이 설치되어 있는지 확인하고, 없으면 설치합니다.

    매개변수:
        ext: 확장 이름 (예: "containerapp")
    """
    try:
        subprocess.run(
            ["az", "extension", "show", "--name", ext],
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8"
        )
    except subprocess.CalledProcessError:
        print(f"[INFO] Azure CLI 확장 '{ext}' 설치 중...", file=sys.stderr)
        subprocess.run(
            ["az", "extension", "add", "--name", ext, "--only-show-errors"],
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8"
        )

def main():
    """독립 실행 테스트"""
    print("=== azure/utils.py 테스트 ===", file=sys.stderr)
    # Azure CLI 설치 확인
    try:
        subprocess.run(["az", "--version"], capture_output=True, check=False)
        print("Azure CLI가 설치되어 있습니다.")
        # ensure_azure_login()  # 실제 로그인 필요 시 주석 해제
    except (subprocess.SubprocessError, FileNotFoundError):
        print("Azure CLI가 설치되지 않았습니다. 테스트 스킵.")
    # get_current_user_object_id 테스트
    user_id = get_current_user_object_id()
    if user_id:
        print(f"현재 사용자 Object ID: {user_id}")
    else:
        print("현재 사용자 Object ID를 가져올 수 없음 (로그인 필요).")
    print("테스트 완료.", file=sys.stderr)

if __name__ == "__main__":
    main()
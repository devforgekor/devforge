#!/usr/bin/env python3
"""azure/functions | Azure Functions: deploy, app settings update, deployment slot create | deploy_functions(),update_function_settings(),create_deployment_slot()"""

import subprocess
import sys
from typing import List

def deploy_functions(resource_group: str, function_app: str, location: str,
                     storage_account: str, plan: str = "consumption") -> bool:
    """
    Functions 배포를 수행합니다.
    
    매개변수:
        resource_group: 리소스 그룹 이름
        function_app: Function App 이름
        location: Azure 지역
        storage_account: Storage Account 이름
        plan: 요금제 (기본값 "consumption")
    
    반환값:
        성공 시 True, 실패 시 False
    """
    print(f"[INFO] Functions 배포 시작: {function_app} (리소스 그룹: {resource_group})", file=sys.stderr)
    
    # Function App 존재 여부 확인
    try:
        subprocess.run(
            ["az", "functionapp", "show", "--name", function_app, "--resource-group", resource_group],
            capture_output=True,
            check=True,
            text=True,
            encoding="utf-8"
        )
        print("[INFO] Function App이 이미 존재합니다.", file=sys.stderr)
        return True
    except subprocess.CalledProcessError:
        pass  # 존재하지 않음
    
    # Storage Account 연결 (필요 시 생성)
    if not storage_account:
        print("[ERROR] Storage Account 이름이 필요합니다.", file=sys.stderr)
        return False
    
    # Function App 생성
    try:
        subprocess.run(
            ["az", "functionapp", "create",
             "--name", function_app,
             "--resource-group", resource_group,
             "--storage-account", storage_account,
             "--consumption-plan-location", location,
             "--runtime", "node",
             "--functions-version", "4",
             "--os-type", "Linux",
             "--only-show-errors",
             "--output", "none"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8"
        )
        print(f"[SUCCESS] Function App 생성 완료: {function_app}", file=sys.stderr)
        return True
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Function App 생성 실패: {e.stderr}", file=sys.stderr)
        return False

def update_function_settings(function_app: str, resource_group: str, settings: List[str]):
    """
    Functions 설정을 업데이트합니다.
    
    매개변수:
        function_app: Function App 이름
        resource_group: 리소스 그룹 이름
        settings: 설정 리스트 (예: ["KEY1=value1", "KEY2=value2"])
    """
    for setting in settings:
        try:
            key, value = setting.split("=", 1)
        except ValueError:
            print(f"[WARN] 잘못된 설정 형식: {setting}", file=sys.stderr)
            continue
        print(f"[INFO] 설정 적용: {key}={value}", file=sys.stderr)
        try:
            subprocess.run(
                ["az", "functionapp", "config", "appsettings", "set",
                 "--name", function_app,
                 "--resource-group", resource_group,
                 "--settings", f"{key}={value}",
                 "--only-show-errors",
                 "--output", "none"],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8"
            )
        except subprocess.CalledProcessError as e:
            print(f"[ERROR] 설정 적용 실패: {e.stderr}", file=sys.stderr)

def create_deployment_slot(function_app: str, resource_group: str, slot_name: str = "staging") -> bool:
    """
    Functions 배포 슬롯을 생성합니다.
    
    매개변수:
        function_app: Function App 이름
        resource_group: 리소스 그룹 이름
        slot_name: 슬롯 이름 (기본값 "staging")
    
    반환값:
        성공 시 True, 실패 시 False
    """
    print(f"[INFO] 배포 슬롯 생성: {slot_name}", file=sys.stderr)
    try:
        subprocess.run(
            ["az", "functionapp", "deployment", "slot", "create",
             "--name", function_app,
             "--resource-group", resource_group,
             "--slot", slot_name,
             "--only-show-errors",
             "--output", "none"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8"
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] 슬롯 생성 실패: {e.stderr}", file=sys.stderr)
        return False

if __name__ == "__main__":
    # 독립 실행 테스트 블록
    print("=== azure/functions.py 테스트 ===", file=sys.stderr)
    print("이 모듈은 실제 Azure Functions 배포를 수행하므로 테스트를 스킵합니다.", file=sys.stderr)
    print("함수 정의 확인:", file=sys.stderr)
    print(f"  deploy_functions: {deploy_functions.__doc__.splitlines()[0]}")
    print(f"  update_function_settings: {update_function_settings.__doc__.splitlines()[0]}")
    print(f"  create_deployment_slot: {create_deployment_slot.__doc__.splitlines()[0]}")
    print("테스트 완료.", file=sys.stderr)
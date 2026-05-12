#!/usr/bin/env python3
"""azure/rbac | RBAC role assignment: assign if missing, auto from deployment outputs, retry | assign_role_if_missing(),auto_assign_rbac_from_outputs(),assign_role_with_retry()"""

import subprocess
import sys
import time
import json
from typing import Optional, Dict

def assign_role_if_missing(assignee: str, role: str, scope: str, desc: str):
    """
    역할이 없으면 할당합니다.
    
    매개변수:
        assignee: 할당 대상 (사용자, 그룹, 서비스 주체)
        role: 역할 이름 (예: "Key Vault Secrets Officer")
        scope: 리소스 범위 (리소스 ID)
        desc: 설명 메시지
    """
    try:
        result = subprocess.run(
            ["az", "role", "assignment", "list", "--assignee", assignee,
             "--scope", scope, "--role", role, "--query", f"[?scope=='{scope}'].id", "-o", "tsv"],
            capture_output=True,
            text=True,
            encoding="utf-8"
        )
        exists = result.stdout.strip()
        if exists:
            print(f"[INFO] {desc} (이미 존재)", file=sys.stderr)
            return
    except subprocess.CalledProcessError as e:
        print(f"[WARN] 역할 확인 실패: {e.stderr}", file=sys.stderr)
    
    print(f"[INFO] {desc}", file=sys.stderr)
    try:
        subprocess.run(
            ["az", "role", "assignment", "create", "--assignee", assignee, "--role", role,
             "--scope", scope, "--only-show-errors", "--output", "none"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8"
        )
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] 역할 할당 실패: {e.stderr}", file=sys.stderr)
        raise

def auto_assign_rbac_from_outputs(deploy_name: str, user_obj_id: str):
    """
    배포 출력에서 리소스 ID를 읽어 자동 RBAC 할당을 수행합니다.
    
    매개변수:
        deploy_name: 배포 이름
        user_obj_id: 사용자 객체 ID
    """
    try:
        result = subprocess.run(
            ["az", "deployment", "sub", "show", "--name", deploy_name,
             "--query", "properties.outputs.resourceIdsForRbac.value", "-o", "json"],
            capture_output=True,
            text=True,
            encoding="utf-8"
        )
        outputs = json.loads(result.stdout) if result.stdout else {}
    except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
        print(f"[WARN] 배포 출력 조회 실패: {e}", file=sys.stderr)
        outputs = {}
    
    role_map: Dict[str, str] = {
        "keyVaultId": "Key Vault Secrets Officer",
        "storageAccountId": "Storage Blob Data Contributor",
        "cosmosAccountId": "DocumentDB Account Contributor",
        "openaiAccountId": "Cognitive Services OpenAI User",
    }
    
    for key, role in role_map.items():
        resource_id = outputs.get(key)
        if resource_id and resource_id != "null":
            assign_role_if_missing(
                user_obj_id, role, resource_id,
                f"현재 사용자 → {role}"
            )

def assign_role_with_retry(assignee: str, role: str, scope: str, desc: str,
                           max_retries: int = 5, retry_delay: int = 5) -> bool:
    """
    역할 할당을 재시도와 함께 수행합니다.
    
    매개변수:
        assignee: 할당 대상
        role: 역할 이름
        scope: 리소스 범위
        desc: 설명 메시지
        max_retries: 최대 재시도 횟수
        retry_delay: 재시도 대기 시간(초)
    
    반환값:
        성공 시 True, 실패 시 False
    """
    try:
        result = subprocess.run(
            ["az", "role", "assignment", "list", "--assignee", assignee,
             "--scope", scope, "--role", role, "--query", f"[?scope=='{scope}'].id", "-o", "tsv"],
            capture_output=True,
            text=True,
            encoding="utf-8"
        )
        exists = result.stdout.strip()
        if exists:
            print(f"[INFO] {desc} (이미 존재)", file=sys.stderr)
            return True
    except subprocess.CalledProcessError as e:
        print(f"[WARN] 역할 확인 실패: {e.stderr}", file=sys.stderr)
    
    print(f"[INFO] {desc}", file=sys.stderr)
    for retry in range(max_retries):
        try:
            subprocess.run(
                ["az", "role", "assignment", "create", "--assignee", assignee, "--role", role,
                 "--scope", scope, "--only-show-errors", "--output", "none"],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8"
            )
            print("[INFO] 역할 할당 성공", file=sys.stderr)
            return True
        except subprocess.CalledProcessError as e:
            print(f"[WARN] 역할 할당 실패, 재시도 {retry+1}/{max_retries}...", file=sys.stderr)
            time.sleep(retry_delay)
    
    print("[ERROR] 역할 할당에 실패했습니다. (전파 지연/권한 문제 가능)", file=sys.stderr)
    return False

if __name__ == "__main__":
    # 독립 실행 테스트 블록
    print("=== azure/rbac.py 테스트 ===", file=sys.stderr)
    print("이 모듈은 실제 Azure RBAC 할당을 수행하므로 테스트를 스킵합니다.", file=sys.stderr)
    print("함수 정의 확인:", file=sys.stderr)
    print(f"  assign_role_if_missing: {assign_role_if_missing.__doc__.splitlines()[0]}")
    print(f"  auto_assign_rbac_from_outputs: {auto_assign_rbac_from_outputs.__doc__.splitlines()[0]}")
    print(f"  assign_role_with_retry: {assign_role_with_retry.__doc__.splitlines()[0]}")
    print("테스트 완료.", file=sys.stderr)
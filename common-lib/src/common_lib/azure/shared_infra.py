#!/usr/bin/env python3
"""azure/shared_infra | shared infra: subscription ID, resource lock, Cosmos+ContainerApps environment | get_subscription_id(),ensure_resource_lock(),ensure_shared_infrastructure()"""

import subprocess
import sys
import os
import json


def get_subscription_id() -> str:
    """Azure 구독 ID를 동적으로 조회합니다."""
    result = subprocess.run(
        ['az', 'account', 'show', '--query', 'id', '-o', 'tsv'],
        capture_output=True, text=True, check=True, encoding='utf-8'
    )
    return result.stdout.strip()


def _parse_resource_id(resource_id: str):
    """Azure 리소스 ID를 파싱하여 리소스 그룹, 타입, 이름을 반환합니다.

    형식: /subscriptions/{sub}/resourceGroups/{rg}/providers/{provider}/{type}/{name}
    """
    parts = resource_id.strip('/').split('/')
    # parts = ['subscriptions', sub, 'resourceGroups', rg, 'providers', provider, type, name]
    rg = parts[3] if len(parts) > 3 else ''
    resource_type = f'{parts[5]}/{parts[6]}' if len(parts) > 6 else ''
    resource_name = parts[7] if len(parts) > 7 else ''
    return rg, resource_type, resource_name


def ensure_resource_lock(resource_id: str, lock_name: str, note: str = '') -> None:
    """지정된 리소스에 CanNotDelete 락을 설정합니다 (없는 경우만).

    매개변수:
        resource_id: Azure 리소스 ID
        lock_name: 락 이름
        note: 락 설명
    """
    rg, resource_type, resource_name = _parse_resource_id(resource_id)
    if not rg or not resource_type or not resource_name:
        print(f"[WARN] 리소스 ID 파싱 실패: {resource_id}", file=sys.stderr)
        return

    try:
        existing = subprocess.run(
            ['az', 'lock', 'list', '--resource-group', rg,
             '--resource-type', resource_type, '--resource-name', resource_name,
             '--query', f'[?name==`{lock_name}`]', '-o', 'json'],
            capture_output=True, text=True, check=True, encoding='utf-8'
        )
        locks = json.loads(existing.stdout) if existing.stdout.strip() else []
        if locks:
            return  # 이미 락이 있음
    except subprocess.CalledProcessError:
        pass

    try:
        subprocess.run(
            ['az', 'lock', 'create', '--name', lock_name,
             '--resource-group', rg,
             '--resource-type', resource_type,
             '--resource-name', resource_name,
             '--lock-type', 'CanNotDelete',
             '--notes', note, '--output', 'none'],
            check=True, capture_output=True, text=True, encoding='utf-8'
        )
        print(f"[INFO] 리소스 락 설정 완료: {lock_name} ({note})", file=sys.stderr)
    except subprocess.CalledProcessError as e:
        print(f"[WARN] 리소스 락 설정 실패: {e.stderr.strip() if e.stderr else e}", file=sys.stderr)


def ensure_shared_infrastructure(location: str, shared_rg: str, shared_cosmos: str, shared_env: str):
    """
    공유 인프라(리소스 그룹, Cosmos DB, Container Apps Environment)가 존재하는지 확인하고 없으면 생성합니다.
    
    매개변수:
        location: Azure 지역 (예: "koreacentral")
        shared_rg: 공유 리소스 그룹 이름
        shared_cosmos: 공유 Cosmos DB 계정 이름
        shared_env: 공유 Container Apps Environment 이름
    
    환경 변수 설정:
        SHARED_COSMOS_ID: 생성된 Cosmos DB의 ID
        SHARED_ENV_ID: 생성된 Container Apps Environment의 ID
    """
    # 리소스 그룹 확인 및 생성
    try:
        subprocess.run(
            ["az", "group", "show", "--name", shared_rg],
            capture_output=True,
            check=True,
            text=True,
            encoding="utf-8"
        )
    except subprocess.CalledProcessError:
        print(f"[INFO] 공유 리소스 그룹({shared_rg}) 생성", file=sys.stderr)
        subprocess.run(
            ["az", "group", "create", "--name", shared_rg, "--location", location, "--output", "none"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8"
        )
    
    # Cosmos DB 확인 및 생성 (무료 계층)
    try:
        subprocess.run(
            ["az", "cosmosdb", "show", "--name", shared_cosmos, "--resource-group", shared_rg],
            capture_output=True,
            check=True,
            text=True,
            encoding="utf-8"
        )
    except subprocess.CalledProcessError:
        print(f"[INFO] 공유 Cosmos DB({shared_cosmos}) 생성 (약 5~10분)", file=sys.stderr)
        subprocess.run(
            ["az", "cosmosdb", "create",
             "--name", shared_cosmos,
             "--resource-group", shared_rg,
             "--locations", f"regionName={location}", "failoverPriority=0",
             "--default-consistency-level", "Session",
             "--enable-free-tier", "true",
             "--kind", "GlobalDocumentDB",
             "--output", "none"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8"
        )
    
    # Container Apps Environment 확인 및 생성
    try:
        subprocess.run(
            ["az", "containerapp", "env", "show", "--name", shared_env, "--resource-group", shared_rg],
            capture_output=True,
            check=True,
            text=True,
            encoding="utf-8"
        )
    except subprocess.CalledProcessError:
        print(f"[INFO] 공유 Container Apps Environment({shared_env}) 생성 시도...", file=sys.stderr)
        result = subprocess.run(
            ["az", "containerapp", "env", "create",
             "--name", shared_env,
             "--resource-group", shared_rg,
             "--location", location,
             "--output", "none"],
            capture_output=True,
            text=True,
            encoding="utf-8"
        )
        if result.returncode != 0:
            # 생성 실패 (이미 다른 CAE가 존재하는 경우 등) - 무시, 호출자가 처리
            print(f"[WARN] CAE 생성 실패 (기존 CAE가 있을 수 있음): {result.stderr.strip().split(chr(10))[-1]}", file=sys.stderr)
    
    # 리소스 ID 획득 및 환경 변수 설정
    try:
        cosmos_id = subprocess.run(
            ["az", "cosmosdb", "show", "--name", shared_cosmos, "--resource-group", shared_rg, "--query", "id", "-o", "tsv"],
            capture_output=True,
            check=True,
            text=True,
            encoding="utf-8"
        ).stdout.strip()
        os.environ["SHARED_COSMOS_ID"] = cosmos_id
    except subprocess.CalledProcessError as e:
        print(f"[WARN] Cosmos DB ID 조회 실패: {e.stderr}", file=sys.stderr)
    
    try:
        env_data = subprocess.run(
            ["az", "containerapp", "env", "show", "--name", shared_env, "--resource-group", shared_rg,
             "--query", "{id:id, name:name}", "-o", "json"],
            capture_output=True,
            check=True,
            text=True,
            encoding="utf-8"
        )
        env_info = json.loads(env_data.stdout)
        os.environ["SHARED_ENV_ID"] = env_info["id"]
        os.environ["SHARED_ENV_NAME"] = env_info["name"]
        os.environ["SHARED_ENV_RG"] = shared_rg
    except subprocess.CalledProcessError as e:
        print(f"[WARN] Container Apps Environment ID 조회 실패: {e.stderr}", file=sys.stderr)

    # 공유 리소스에 삭제 방지 락 설정
    cosmos_id_val = os.environ.get("SHARED_COSMOS_ID", "")
    env_id_val = os.environ.get("SHARED_ENV_ID", "")
    if cosmos_id_val:
        ensure_resource_lock(cosmos_id_val, f"lock-{shared_cosmos}", "공유 Cosmos DB 삭제 방지")
    if env_id_val:
        ensure_resource_lock(env_id_val, f"lock-{shared_env}", "공유 Container Apps Environment 삭제 방지")

if __name__ == "__main__":
    # 독립 실행 테스트 블록
    print("=== azure/shared_infra.py 테스트 ===", file=sys.stderr)
    print("이 모듈은 실제 Azure 리소스를 생성하므로 테스트를 스킵합니다.", file=sys.stderr)
    print("함수 정의 확인:", file=sys.stderr)
    print(f"  ensure_shared_infrastructure: {ensure_shared_infrastructure.__doc__.splitlines()[0]}")
    print("테스트 완료.", file=sys.stderr)
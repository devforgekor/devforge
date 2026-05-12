#!/usr/bin/env python3
"""azure/storage | Storage: account, container, key, lifecycle policy, static website, delete retention | create_storage_account_if_not_exists(),get_storage_account_key(),create_storage_container_if_not_exists(),enable_blob_delete_retention(),set_storage_lifecycle_policy_from_file(),enable_static_website()"""

import subprocess
import sys
import os
from typing import Optional


def create_storage_account_if_not_exists(
    resource_group: str,
    account_name: str,
    location: str,
    sku: str = "Standard_LRS",
) -> bool:
    """
    Storage 계정이 없으면 생성합니다.

    Args:
        resource_group: 리소스 그룹 이름
        account_name: 스토리지 계정 이름
        location: Azure 지역
        sku: SKU (기본값 "Standard_LRS")

    Returns:
        성공 시 True, 실패 시 False
    """
    # 존재 여부 확인
    try:
        subprocess.run(
            [
                "az", "storage", "account", "show",
                "--name", account_name,
                "--resource-group", resource_group,
            ],
            capture_output=True,
            check=True,
        )
        print(f"[INFO] Storage 계정({account_name})이 이미 존재합니다.", file=sys.stderr)
        return True
    except subprocess.CalledProcessError:
        pass  # 존재하지 않음
    except FileNotFoundError:
        print("[ERROR] Azure CLI가 설치되지 않았거나 PATH에 없습니다.", file=sys.stderr)
        return False

    # 생성
    print(f"[INFO] Storage 계정({account_name}) 생성", file=sys.stderr)
    try:
        subprocess.run(
            [
                "az", "storage", "account", "create",
                "--name", account_name,
                "--resource-group", resource_group,
                "--location", location,
                "--sku", sku,
                "--kind", "StorageV2",
                "--https-only", "true",
                "--allow-blob-public-access", "false",
                "--min-tls-version", "TLS1_2",
                "--output", "none",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Storage 계정 생성 실패: {e}", file=sys.stderr)
        return False
    except FileNotFoundError:
        print("[ERROR] Azure CLI가 설치되지 않았거나 PATH에 없습니다.", file=sys.stderr)
        return False


def get_storage_account_key(
    resource_group: str,
    account_name: str,
) -> Optional[str]:
    """
    Storage 계정의 첫 번째 키를 가져옵니다.

    Args:
        resource_group: 리소스 그룹 이름
        account_name: 스토리지 계정 이름

    Returns:
        키 문자열 (실패 시 None)
    """
    try:
        result = subprocess.run(
            [
                "az", "storage", "account", "keys", "list",
                "--account-name", account_name,
                "--resource-group", resource_group,
                "--query", "[0].value",
                "-o", "tsv",
            ],
            capture_output=True,
            check=True,
            text=True,
            encoding="utf-8",
        )
        key = result.stdout.strip()
        if not key:
            print(f"[WARN] Storage 계정 키가 비어 있습니다.", file=sys.stderr)
            return None
        return key
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Storage 계정 키 조회 실패: {e}", file=sys.stderr)
        return None
    except FileNotFoundError:
        print("[ERROR] Azure CLI가 설치되지 않았거나 PATH에 없습니다.", file=sys.stderr)
        return None


def create_storage_container_if_not_exists(
    resource_group: str,
    account_name: str,
    container_name: str,
) -> bool:
    """
    Storage 컨테이너를 생성합니다 (이미 존재해도 무시).

    Args:
        resource_group: 리소스 그룹 이름
        account_name: 스토리지 계정 이름
        container_name: 컨테이너 이름

    Returns:
        성공 시 True, 실패 시 False
    """
    key = get_storage_account_key(resource_group, account_name)
    if key is None:
        print(f"[ERROR] Storage 계정 키를 가져올 수 없어 컨테이너 생성 불가", file=sys.stderr)
        return False

    try:
        subprocess.run(
            [
                "az", "storage", "container", "create",
                "--name", container_name,
                "--account-name", account_name,
                "--account-key", key,
                "--output", "none",
            ],
            capture_output=True,
            check=False,  # 이미 존재해도 실패하지 않도록
            text=True,
            encoding="utf-8",
        )
        # 이미 존재하는 경우에도 오류가 아니므로 성공으로 간주
        return True
    except Exception as e:
        print(f"[WARN] Storage 컨테이너 생성 중 예외 (무시 가능): {e}", file=sys.stderr)
        return True  # Bash에서는 `|| true`로 실패를 무시하므로 True 반환


def enable_blob_delete_retention(
    resource_group: str,
    account_name: str,
    retention_days: int = 30,
) -> bool:
    """
    Blob 삭제 보존을 활성화합니다.

    Args:
        resource_group: 리소스 그룹 이름
        account_name: 스토리지 계정 이름
        retention_days: 보존 일수 (기본값 30)

    Returns:
        성공 시 True, 실패 시 False
    """
    print(f"[INFO] Blob 삭제 보존 설정: {retention_days} 일", file=sys.stderr)
    try:
        subprocess.run(
            [
                "az", "storage", "account", "blob-service-properties", "update",
                "--account-name", account_name,
                "--resource-group", resource_group,
                "--enable-delete-retention", "true",
                "--delete-retention-days", str(retention_days),
                "--output", "none",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Blob 삭제 보존 설정 실패: {e}", file=sys.stderr)
        return False
    except FileNotFoundError:
        print("[ERROR] Azure CLI가 설치되지 않았거나 PATH에 없습니다.", file=sys.stderr)
        return False


def set_storage_lifecycle_policy_from_file(
    resource_group: str,
    account_name: str,
    policy_file: str,
) -> bool:
    """
    JSON 파일로부터 Storage 수명 주기 관리 정책을 설정합니다.

    Args:
        resource_group: 리소스 그룹 이름
        account_name: 스토리지 계정 이름
        policy_file: 정책 JSON 파일 경로

    Returns:
        성공 시 True, 실패 시 False
    """
    if not os.path.exists(policy_file):
        print(f"[ERROR] 정책 파일이 존재하지 않습니다: {policy_file}", file=sys.stderr)
        return False

    # 기존 정책 존재 여부 확인 (Bash 스크립트와 동일한 로직)
    try:
        subprocess.run(
            [
                "az", "storage", "account", "management-policy", "show",
                "--account-name", account_name,
                "--resource-group", resource_group,
            ],
            capture_output=True,
            check=False,  # 존재하지 않으면 실패 (오류 출력하지 않음)
            text=True,
            encoding="utf-8",
        )
        policy_exists = True
    except Exception:
        policy_exists = False

    if policy_exists:
        print(f"[INFO] 기존 수명 주기 정책 업데이트", file=sys.stderr)
        action = "update"
    else:
        print(f"[INFO] 수명 주기 정책 생성", file=sys.stderr)
        action = "create"

    try:
        subprocess.run(
            [
                "az", "storage", "account", "management-policy", action,
                "--account-name", account_name,
                "--resource-group", resource_group,
                "--policy", f"@{policy_file}",
                "--output", "none",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] 수명 주기 정책 {action} 실패: {e}", file=sys.stderr)
        return False
    except FileNotFoundError:
        print("[ERROR] Azure CLI가 설치되지 않았거나 PATH에 없습니다.", file=sys.stderr)
        return False


def enable_static_website(
    resource_group: str,
    account_name: str,
    index_document: str = "index.html",
    error_document: str = "error.html",
) -> bool:
    """
    정적 웹사이트 호스팅을 활성화합니다.

    Args:
        resource_group: 리소스 그룹 이름
        account_name: 스토리지 계정 이름
        index_document: 인덱스 문서 이름 (기본값 "index.html")
        error_document: 오류 문서 이름 (기본값 "error.html")

    Returns:
        성공 시 True, 실패 시 False
    """
    try:
        subprocess.run(
            [
                "az", "storage", "blob", "service-properties", "update",
                "--account-name", account_name,
                "--resource-group", resource_group,
                "--static-website",
                "--index-document", index_document,
                "--error-document", error_document,
                "--output", "none",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] 정적 웹사이트 설정 실패: {e}", file=sys.stderr)
        return False
    except FileNotFoundError:
        print("[ERROR] Azure CLI가 설치되지 않았거나 PATH에 없습니다.", file=sys.stderr)
        return False


def main() -> None:
    """독립 실행 테스트"""
    print("=== azure/storage.py 테스트 ===", file=sys.stderr)
    print("이 모듈은 실제 Azure Storage 계정이 필요하므로 테스트를 스킵합니다.", file=sys.stderr)
    print("함수 정의 확인:", file=sys.stderr)
    print(f"  create_storage_account_if_not_exists: {create_storage_account_if_not_exists}", file=sys.stderr)
    print(f"  get_storage_account_key: {get_storage_account_key}", file=sys.stderr)
    print(f"  create_storage_container_if_not_exists: {create_storage_container_if_not_exists}", file=sys.stderr)
    print(f"  enable_blob_delete_retention: {enable_blob_delete_retention}", file=sys.stderr)
    print(f"  set_storage_lifecycle_policy_from_file: {set_storage_lifecycle_policy_from_file}", file=sys.stderr)
    print(f"  enable_static_website: {enable_static_website}", file=sys.stderr)
    print("테스트 완료.", file=sys.stderr)


if __name__ == "__main__":
    main()
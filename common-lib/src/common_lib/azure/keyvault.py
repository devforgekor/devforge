#!/usr/bin/env python3
"""azure/keyvault | Key Vault: create, set/update secrets, RBAC policy for managed identity, get URI | create_keyvault_if_not_exists(),set_keyvault_secret_if_missing(),set_keyvault_secret(),add_keyvault_policy_for_identity(),get_keyvault_uri(),get_keyvault_secret()"""

import subprocess
import sys
import time
from typing import Optional


def create_keyvault_if_not_exists(
    resource_group: str,
    vault_name: str,
    location: str,
    sku: str = "standard",
) -> bool:
    """
    Key Vault가 없으면 생성합니다.

    Args:
        resource_group: 리소스 그룹 이름
        vault_name: 볼트 이름
        location: Azure 지역
        sku: SKU (기본값 "standard")

    Returns:
        성공 시 True, 실패 시 False
    """
    # Soft-Delete 상태 확인
    try:
        result = subprocess.run(
            [
                "az", "keyvault", "list-deleted",
                "--query", f"[?name=='{vault_name}'].name",
                "-o", "tsv",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        deleted_vault = result.stdout.strip()
        if deleted_vault:
            print(f"[ERROR] Key Vault '{vault_name}' 가 Soft-Delete 상태입니다.", file=sys.stderr)
            print(f"  az keyvault purge --name {vault_name} --location {location}", file=sys.stderr)
            return False
    except subprocess.CalledProcessError:
        pass

    # 존재 확인
    try:
        subprocess.run(
            [
                "az", "keyvault", "show",
                "--name", vault_name,
                "--resource-group", resource_group,
            ],
            capture_output=True,
            check=True,
        )
        print(f"[INFO] Key Vault({vault_name})이 이미 존재합니다.", file=sys.stderr)
        return True
    except subprocess.CalledProcessError:
        pass  # 존재하지 않음

    # 생성
    print(f"[INFO] Key Vault({vault_name}) 생성", file=sys.stderr)
    try:
        subprocess.run(
            [
                "az", "keyvault", "create",
                "--name", vault_name,
                "--resource-group", resource_group,
                "--location", location,
                "--sku", sku,
                "--enable-rbac-authorization", "false",
                "--output", "none",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        # DNS 전파를 위한 짧은 대기
        time.sleep(5)
        return True
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Key Vault 생성 실패: {e}", file=sys.stderr)
        return False
    except FileNotFoundError:
        print("[ERROR] Azure CLI가 설치되지 않았거나 PATH에 없습니다.", file=sys.stderr)
        return False


def set_keyvault_secret_if_missing(
    vault_name: str,
    secret_name: str,
    secret_value: str,
    allow_default_secrets: bool = False,
) -> bool:
    """
    Key Vault에 비밀을 설정합니다 (이미 존재하면 덮어쓰지 않음).

    Args:
        vault_name: 볼트 이름
        secret_name: 비밀 이름
        secret_value: 비밀 값
        allow_default_secrets: 기본값(플레이스홀더) 허용 여부

    Returns:
        성공 시 True, 실패 시 False
    """
    # 기존 값 확인
    existing = ""
    try:
        result = subprocess.run(
            [
                "az", "keyvault", "secret", "show",
                "--vault-name", vault_name,
                "--name", secret_name,
                "--query", "value",
                "-o", "tsv",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        existing = result.stdout.strip()
    except subprocess.CalledProcessError:
        pass

    if not existing:
        # 기본값(플레이스홀더) 검사
        placeholder_values = ["CHANGE_ME", "01012345678", "https://your-openai.openai.azure.com/"]
        if secret_value in placeholder_values and not allow_default_secrets:
            print(f"[ERROR] 비밀 '{secret_name}' 에 기본값(플레이스홀더)이 설정되었습니다.", file=sys.stderr)
            print("운영 환경에 맞는 실제 값으로 변경하거나 ALLOW_DEFAULT_SECRETS=true 로 실행하세요.", file=sys.stderr)
            return False
        elif secret_value in placeholder_values and allow_default_secrets:
            print(f"[WARN] 비밀 '{secret_name}' 이 기본값으로 생성되었습니다.", file=sys.stderr)

        # 비밀 설정
        try:
            subprocess.run(
                [
                    "az", "keyvault", "secret", "set",
                    "--vault-name", vault_name,
                    "--name", secret_name,
                    "--value", secret_value,
                    "--output", "none",
                ],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            print(f"  - {secret_name} 생성")
            return True
        except subprocess.CalledProcessError as e:
            print(f"[ERROR] 비밀 설정 실패: {e}", file=sys.stderr)
            return False
    else:
        print(f"  - {secret_name} (기존 값 유지)")
        return True


def set_keyvault_secret(
    vault_name: str,
    secret_name: str,
    secret_value: str,
) -> bool:
    """
    Key Vault에 비밀을 설정합니다 (항상 덮어씀).

    Args:
        vault_name: 볼트 이름
        secret_name: 비밀 이름
        secret_value: 비밀 값

    Returns:
        성공 시 True, 실패 시 False
    """
    try:
        subprocess.run(
            [
                "az", "keyvault", "secret", "set",
                "--vault-name", vault_name,
                "--name", secret_name,
                "--value", secret_value,
                "--output", "none",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] 비밀 설정 실패: {e}", file=sys.stderr)
        return False
    except FileNotFoundError:
        return False


def add_keyvault_policy_for_identity(
    vault_name: str,
    resource_group: str,
    object_id: str,
) -> bool:
    """
    Key Vault 정책을 추가합니다 (Managed Identity에 비밀 읽기 권한).

    Args:
        vault_name: 볼트 이름
        resource_group: 리소스 그룹 이름
        object_id: 관리 ID 객체 ID

    Returns:
        성공 시 True, 실패 시 False
    """
    # 기존 정책 확인
    existing = ""
    try:
        result = subprocess.run(
            [
                "az", "keyvault", "show",
                "--name", vault_name,
                "--resource-group", resource_group,
                "--query", f"properties.accessPolicies[?objectId=='{object_id}'].objectId",
                "-o", "tsv",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        existing = result.stdout.strip()
    except subprocess.CalledProcessError:
        pass

    if not existing:
        print(f"[INFO] Key Vault 정책 추가: objectId={object_id}", file=sys.stderr)
        # 재시도 로직
        max_retries = 5
        retry_delay = 5
        for i in range(max_retries):
            try:
                subprocess.run(
                    [
                        "az", "keyvault", "set-policy",
                        "--name", vault_name,
                        "--resource-group", resource_group,
                        "--object-id", object_id,
                        "--secret-permissions", "get", "list",
                        "--output", "none",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                )
                return True
            except subprocess.CalledProcessError:
                if i < max_retries - 1:
                    time.sleep(retry_delay)
                    continue
                else:
                    print("[ERROR] Key Vault 정책 설정에 실패했습니다. (전파 지연/권한 문제 가능)", file=sys.stderr)
                    return False
    else:
        print("[INFO] Key Vault 정책이 이미 존재합니다.", file=sys.stderr)
        return True


def get_keyvault_uri(vault_name: str) -> str:
    """
    Key Vault URI를 가져옵니다.

    Args:
        vault_name: 볼트 이름

    Returns:
        URI 문자열
    """
    return f"https://{vault_name}.vault.azure.net"


def get_keyvault_secret(
    vault_name: str,
    secret_name: str,
) -> Optional[str]:
    """
    비밀 값을 가져옵니다 (없으면 빈 문자열).

    Args:
        vault_name: 볼트 이름
        secret_name: 비밀 이름

    Returns:
        비밀 값 문자열, 실패 시 None
    """
    try:
        result = subprocess.run(
            [
                "az", "keyvault", "secret", "show",
                "--vault-name", vault_name,
                "--name", secret_name,
                "--query", "value",
                "-o", "tsv",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return None
    except FileNotFoundError:
        return None


def main() -> None:
    """독립 실행 테스트"""
    print("=== azure/keyvault.py 테스트 ===", file=sys.stderr)
    print("이 모듈은 실제 Key Vault가 필요하므로 테스트를 스킵합니다.", file=sys.stderr)
    print("함수 정의 확인:", file=sys.stderr)
    print("  create_keyvault_if_not_exists", file=sys.stderr)
    print("  set_keyvault_secret_if_missing", file=sys.stderr)
    print("  set_keyvault_secret", file=sys.stderr)
    print("  add_keyvault_policy_for_identity", file=sys.stderr)
    print("  get_keyvault_uri", file=sys.stderr)
    print("  get_keyvault_secret", file=sys.stderr)
    print("테스트 완료.", file=sys.stderr)


if __name__ == "__main__":
    main()
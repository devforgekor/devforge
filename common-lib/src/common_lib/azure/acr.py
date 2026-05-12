#!/usr/bin/env python3
"""azure/acr | ACR build/push, login server, exists check, create, resource ID | acr_build_and_push(),acr_login_server(),acr_exists(),create_acr_if_not_exists(),get_acr_resource_id()"""

import subprocess
import sys
from typing import Optional


def acr_build_and_push(
    registry_name: str,
    image_name: str,
    dockerfile_path: str,
    context_path: str,
) -> bool:
    """
    ACR 이미지 빌드 및 푸시 (az acr build)

    Args:
        registry_name: 레지스트리 이름
        image_name: 이미지 이름 (태그 포함 가능)
        dockerfile_path: Dockerfile 경로
        context_path: 빌드 컨텍스트 경로

    Returns:
        성공 시 True, 실패 시 False
    """
    print(f"[INFO] ACR 이미지 빌드: {image_name}", file=sys.stderr)
    print(f"  레지스트리: {registry_name}", file=sys.stderr)
    print(f"  컨텍스트: {context_path}", file=sys.stderr)

    try:
        subprocess.run(
            [
                "az", "acr", "build",
                "--registry", registry_name,
                "--image", image_name,
                "--file", dockerfile_path,
                context_path,
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] ACR 이미지 빌드 실패: {e}", file=sys.stderr)
        return False
    except FileNotFoundError:
        print("[ERROR] Azure CLI가 설치되지 않았거나 PATH에 없습니다.", file=sys.stderr)
        return False


def acr_login_server(registry_name: str) -> Optional[str]:
    """
    ACR 로그인 서버를 가져옵니다.

    Args:
        registry_name: 레지스트리 이름

    Returns:
        로그인 서버 문자열 (실패 시 None)
    """
    try:
        result = subprocess.run(
            ["az", "acr", "show", "--name", registry_name, "--query", "loginServer", "-o", "tsv"],
            capture_output=True,
            check=True,
            text=True,
            encoding="utf-8",
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return None
    except FileNotFoundError:
        print("[ERROR] Azure CLI가 설치되지 않았거나 PATH에 없습니다.", file=sys.stderr)
        return None


def acr_exists(registry_name: str) -> bool:
    """
    ACR이 존재하는지 확인합니다.

    Args:
        registry_name: 레지스트리 이름

    Returns:
        존재하면 True, 아니면 False
    """
    try:
        subprocess.run(
            ["az", "acr", "show", "--name", registry_name],
            capture_output=True,
            check=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False
    except FileNotFoundError:
        return False


def create_acr_if_not_exists(
    resource_group: str,
    registry_name: str,
    location: str,
    sku: str = "Basic",
) -> None:
    """
    ACR을 생성합니다 (없을 경우).

    Args:
        resource_group: 리소스 그룹 이름
        registry_name: 레지스트리 이름
        location: Azure 지역
        sku: SKU (기본값 "Basic")
    """
    # 존재 여부 확인
    try:
        subprocess.run(
            [
                "az", "acr", "show",
                "--name", registry_name,
                "--resource-group", resource_group,
            ],
            capture_output=True,
            check=True,
        )
        print(f"[INFO] ACR({registry_name})이 이미 존재합니다.", file=sys.stderr)
        return
    except subprocess.CalledProcessError:
        pass  # 존재하지 않음

    # 생성
    print(f"[INFO] ACR({registry_name}) 생성", file=sys.stderr)
    try:
        subprocess.run(
            [
                "az", "acr", "create",
                "--name", registry_name,
                "--resource-group", resource_group,
                "--sku", sku,
                "--location", location,
                "--admin-enabled", "false",
                "--output", "none",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] ACR 생성 실패: {e}", file=sys.stderr)
        sys.exit(1)


def get_acr_resource_id(resource_group: str, registry_name: str) -> Optional[str]:
    """
    ACR 리소스 ID를 가져옵니다.

    Args:
        resource_group: 리소스 그룹 이름
        registry_name: 레지스트리 이름

    Returns:
        리소스 ID 문자열 (실패 시 None)
    """
    try:
        result = subprocess.run(
            [
                "az", "acr", "show",
                "--name", registry_name,
                "--resource-group", resource_group,
                "--query", "id",
                "-o", "tsv",
            ],
            capture_output=True,
            check=True,
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
    print("=== azure/acr.py 테스트 ===", file=sys.stderr)
    print("ACR 테스트는 실제 Azure 리소스가 필요하므로 스킵합니다.", file=sys.stderr)
    print("함수 정의 확인:", file=sys.stderr)
    print("  acr_build_and_push", file=sys.stderr)
    print("  acr_login_server", file=sys.stderr)
    print("  acr_exists", file=sys.stderr)
    print("테스트 완료.", file=sys.stderr)


if __name__ == "__main__":
    main()
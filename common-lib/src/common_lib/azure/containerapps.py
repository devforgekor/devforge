#!/usr/bin/env python3
"""azure/containerapps | Container Apps: image update, revision wait, FQDN, env create, managed identity | update_container_app_image(),get_revision_running_state(),wait_for_revision_ready(),get_container_app_fqdn(),create_containerapp_environment_if_not_exists(),create_containerapp_if_not_exists(),get_containerapp_managed_identity_principal_id()"""

import subprocess
import sys
import time
from typing import Optional, List


def update_container_app_image(
    app_name: str,
    resource_group: str,
    image: str,
) -> bool:
    """
    컨테이너 앱 이미지를 업데이트합니다.

    Args:
        app_name: 컨테이너 앱 이름
        resource_group: 리소스 그룹 이름
        image: 새 이미지 참조

    Returns:
        성공 시 True, 실패 시 False
    """
    print(f"[INFO] 컨테이너 앱 이미지 업데이트: {app_name}", file=sys.stderr)
    print(f"  이미지: {image}", file=sys.stderr)

    try:
        subprocess.run(
            [
                "az", "containerapp", "update",
                "--name", app_name,
                "--resource-group", resource_group,
                "--image", image,
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] 컨테이너 앱 업데이트 실패: {e}", file=sys.stderr)
        return False
    except FileNotFoundError:
        print("[ERROR] Azure CLI가 설치되지 않았거나 PATH에 없습니다.", file=sys.stderr)
        return False


def get_revision_running_state(
    app_name: str,
    resource_group: str,
) -> Optional[str]:
    """
    활성 리비전의 실행 상태를 가져옵니다.

    Args:
        app_name: 컨테이너 앱 이름
        resource_group: 리소스 그룹 이름

    Returns:
        Running 상태 문자열 (예: "Running"), 없으면 None
    """
    try:
        result = subprocess.run(
            [
                "az", "containerapp", "revision", "list",
                "--name", app_name,
                "--resource-group", resource_group,
                "--query", "[?properties.active].properties.runningState",
                "-o", "tsv",
            ],
            capture_output=True,
            check=True,
            text=True,
            encoding="utf-8",
        )
        output = result.stdout.strip()
        lines = output.splitlines()
        if lines:
            return lines[0]
        else:
            return None
    except subprocess.CalledProcessError:
        return None
    except FileNotFoundError:
        return None


def wait_for_revision_ready(
    app_name: str,
    resource_group: str,
    max_wait: int = 90,
    interval: int = 10,
) -> bool:
    """
    리비전이 Running 상태가 될 때까지 대기합니다.

    Args:
        app_name: 컨테이너 앱 이름
        resource_group: 리소스 그룹 이름
        max_wait: 최대 대기 시간 (초)
        interval: 확인 간격 (초)

    Returns:
        성공 시 True, 시간 초과 시 False
    """
    print(f"[INFO] 리비전 준비 완료 대기 (최대 {max_wait}초)", file=sys.stderr)
    elapsed = 0
    revision_status = None

    while elapsed < max_wait:
        revision_status = get_revision_running_state(app_name, resource_group)
        if revision_status == "Running":
            print("  리비전 상태: Running ✓", file=sys.stderr)
            return True
        print(f"  대기 중... ({elapsed}s / {max_wait}s)", file=sys.stderr)
        time.sleep(interval)
        elapsed += interval

    if revision_status != "Running":
        print(
            f"[WARN] 리비전이 {max_wait}초 안에 Running 상태가 되지 않았습니다.",
            file=sys.stderr,
        )
        print(
            f"       'az containerapp revision list -n {app_name} -g {resource_group}' 로 직접 확인하세요.",
            file=sys.stderr,
        )
        return False
    return True


def get_container_app_fqdn(
    app_name: str,
    resource_group: str,
) -> Optional[str]:
    """
    컨테이너 앱의 FQDN을 가져옵니다.

    Args:
        app_name: 컨테이너 앱 이름
        resource_group: 리소스 그룹 이름

    Returns:
        FQDN 문자열, 없으면 None
    """
    try:
        result = subprocess.run(
            [
                "az", "containerapp", "show",
                "--name", app_name,
                "--resource-group", resource_group,
                "--query", "properties.configuration.ingress.fqdn",
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


def create_containerapp_environment_if_not_exists(
    resource_group: str,
    environment_name: str,
    location: str,
) -> None:
    """
    Container Apps Environment를 생성합니다 (없을 경우).

    Args:
        resource_group: 리소스 그룹 이름
        environment_name: 환경 이름
        location: Azure 지역
    """
    # Log Analytics 공급자 등록
    try:
        subprocess.run(
            ["az", "provider", "register", "-n", "Microsoft.OperationalInsights", "--wait", "--output", "none"],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError:
        print("[WARN] Log Analytics 공급자 등록 실패 (무시)", file=sys.stderr)

    # 환경 존재 확인
    try:
        subprocess.run(
            [
                "az", "containerapp", "env", "show",
                "--name", environment_name,
                "--resource-group", resource_group,
            ],
            capture_output=True,
            check=True,
        )
        print(f"[INFO] Container Apps Environment({environment_name})이 이미 존재합니다.", file=sys.stderr)
        return
    except subprocess.CalledProcessError:
        pass  # 존재하지 않음

    # 환경 생성
    print(f"[INFO] Container Apps Environment({environment_name}) 생성", file=sys.stderr)
    try:
        subprocess.run(
            [
                "az", "containerapp", "env", "create",
                "--name", environment_name,
                "--resource-group", resource_group,
                "--location", location,
                "--output", "none",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Container Apps Environment 생성 실패: {e}", file=sys.stderr)
        sys.exit(1)


def create_containerapp_if_not_exists(
    resource_group: str,
    app_name: str,
    environment_name: str,
    location: str,
    image: str = "mcr.microsoft.com/azuredocs/containerapps-helloworld:latest",
    target_port: int = 80,
    min_replicas: int = 0,
    max_replicas: int = 2,
) -> None:
    """
    Container App을 생성합니다 (없을 경우).

    Args:
        resource_group: 리소스 그룹 이름
        app_name: 앱 이름
        environment_name: 환경 이름
        location: Azure 지역
        image: 컨테이너 이미지
        target_port: 대상 포트
        min_replicas: 최소 복제본 수
        max_replicas: 최대 복제본 수
    """
    # 존재 확인
    try:
        subprocess.run(
            [
                "az", "containerapp", "show",
                "--name", app_name,
                "--resource-group", resource_group,
            ],
            capture_output=True,
            check=True,
        )
        print(f"[INFO] Container App({app_name})이 이미 존재합니다.", file=sys.stderr)
        return
    except subprocess.CalledProcessError:
        pass  # 존재하지 않음

    # 생성
    print(f"[INFO] Container App({app_name}) 생성", file=sys.stderr)
    try:
        subprocess.run(
            [
                "az", "containerapp", "create",
                "--name", app_name,
                "--resource-group", resource_group,
                "--environment", environment_name,
                "--image", image,
                "--target-port", str(target_port),
                "--ingress", "external",
                "--min-replicas", str(min_replicas),
                "--max-replicas", str(max_replicas),
                "--system-assigned",
                "--output", "none",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Container App 생성 실패: {e}", file=sys.stderr)
        sys.exit(1)


def get_containerapp_managed_identity_principal_id(
    resource_group: str,
    app_name: str,
    max_retries: int = 3,
    retry_delay: int = 10,
) -> Optional[str]:
    """
    Container App의 Managed Identity Principal ID를 가져옵니다.

    Args:
        resource_group: 리소스 그룹 이름
        app_name: 앱 이름
        max_retries: 최대 재시도 횟수
        retry_delay: 재시도 간 지연 (초)

    Returns:
        Principal ID 문자열, 실패 시 None
    """
    principal_id = None
    for i in range(1, max_retries + 1):
        try:
            result = subprocess.run(
                [
                    "az", "containerapp", "show",
                    "--name", app_name,
                    "--resource-group", resource_group,
                    "--query", "identity.principalId",
                    "-o", "tsv",
                ],
                capture_output=True,
                check=True,
                text=True,
                encoding="utf-8",
            )
            principal_id = result.stdout.strip()
            if principal_id and principal_id != "null":
                return principal_id
        except subprocess.CalledProcessError:
            pass
        print(f"[INFO] Principal ID 아직 준비되지 않음. 재시도 {i}/{max_retries}...", file=sys.stderr)
        time.sleep(retry_delay)
    print("[ERROR] Container App의 Managed Identity를 가져올 수 없습니다.", file=sys.stderr)
    return None


def main() -> None:
    """독립 실행 테스트"""
    print("=== azure/containerapps.py 테스트 ===", file=sys.stderr)
    print("Container Apps 테스트는 실제 Azure 리소스가 필요하므로 스킵합니다.", file=sys.stderr)
    print("함수 정의 확인:", file=sys.stderr)
    print("  update_container_app_image", file=sys.stderr)
    print("  get_revision_running_state", file=sys.stderr)
    print("  wait_for_revision_ready", file=sys.stderr)
    print("  get_container_app_fqdn", file=sys.stderr)
    print("테스트 완료.", file=sys.stderr)


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""azure/cosmos | Cosmos DB: account, SQL db, container, TTL, indexing policy, connection string | create_cosmos_account_if_not_exists(),create_cosmos_sql_database_if_not_exists(),create_cosmos_sql_container_if_not_exists(),set_cosmos_container_ttl(),set_cosmos_container_indexing_policy(),get_cosmos_connection_string()"""

import subprocess
import sys
from typing import Optional


def create_cosmos_account_if_not_exists(
    resource_group: str,
    account_name: str,
    location: str,
    free_tier: bool = True,
) -> bool:
    """
    Cosmos DB 계정이 없으면 생성합니다 (Free Tier 가능).

    Args:
        resource_group: 리소스 그룹 이름
        account_name: 계정 이름
        location: Azure 지역
        free_tier: Free Tier 활성화 여부 (기본값 True)

    Returns:
        성공 시 True, 실패 시 False
    """
    # 존재 확인
    try:
        subprocess.run(
            [
                "az", "cosmosdb", "show",
                "--name", account_name,
                "--resource-group", resource_group,
            ],
            capture_output=True,
            check=True,
        )
        print(f"[INFO] Cosmos DB 계정({account_name})이 이미 존재합니다.", file=sys.stderr)
        return True
    except subprocess.CalledProcessError:
        pass  # 존재하지 않음

    # 생성
    print(f"[INFO] Cosmos DB 계정({account_name}) 생성 (약 5~10분)", file=sys.stderr)
    cmd = [
        "az", "cosmosdb", "create",
        "--name", account_name,
        "--resource-group", resource_group,
        "--locations", f"regionName={location}",
        "--default-consistency-level", "Session",
        "--output", "none",
    ]
    if free_tier:
        cmd.append("--enable-free-tier")
        cmd.append("true")

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, encoding="utf-8")
        return True
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Cosmos DB 생성 실패: {e}", file=sys.stderr)
        print("힌트: --enable-free-tier true 는 구독당 1개만 허용됩니다.", file=sys.stderr)
        print("이미 Free Tier를 사용 중이면 해당 옵션을 제거하거나 기존 계정을 사용하세요.", file=sys.stderr)
        return False
    except FileNotFoundError:
        print("[ERROR] Azure CLI가 설치되지 않았거나 PATH에 없습니다.", file=sys.stderr)
        return False


def create_cosmos_sql_database_if_not_exists(
    resource_group: str,
    account_name: str,
    database_name: str,
    throughput: int = 400,
) -> None:
    """
    SQL 데이터베이스를 생성합니다 (없을 경우).

    Args:
        resource_group: 리소스 그룹 이름
        account_name: 계정 이름
        database_name: 데이터베이스 이름
        throughput: 처리량 (RU/s)
    """
    # 존재 확인
    try:
        subprocess.run(
            [
                "az", "cosmosdb", "sql", "database", "show",
                "--account-name", account_name,
                "--resource-group", resource_group,
                "--name", database_name,
            ],
            capture_output=True,
            check=True,
        )
        print(f"[INFO] SQL 데이터베이스({database_name})이 이미 존재합니다.", file=sys.stderr)
        return
    except subprocess.CalledProcessError:
        pass

    # 생성
    print(f"[INFO] SQL 데이터베이스({database_name}) 생성", file=sys.stderr)
    try:
        subprocess.run(
            [
                "az", "cosmosdb", "sql", "database", "create",
                "--account-name", account_name,
                "--resource-group", resource_group,
                "--name", database_name,
                "--throughput", str(throughput),
                "--output", "none",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] SQL 데이터베이스 생성 실패: {e}", file=sys.stderr)
        sys.exit(1)


def create_cosmos_sql_container_if_not_exists(
    resource_group: str,
    account_name: str,
    database_name: str,
    container_name: str,
    partition_key: str = "/id",
    throughput: int = 400,
) -> None:
    """
    SQL 컨테이너를 생성합니다 (없을 경우).

    Args:
        resource_group: 리소스 그룹 이름
        account_name: 계정 이름
        database_name: 데이터베이스 이름
        container_name: 컨테이너 이름
        partition_key: 파티션 키 경로 (기본값 "/id")
        throughput: 처리량 (RU/s)
    """
    # 존재 확인
    try:
        subprocess.run(
            [
                "az", "cosmosdb", "sql", "container", "show",
                "--account-name", account_name,
                "--resource-group", resource_group,
                "--database-name", database_name,
                "--name", container_name,
            ],
            capture_output=True,
            check=True,
        )
        print(f"[INFO] SQL 컨테이너({container_name})이 이미 존재합니다.", file=sys.stderr)
        return
    except subprocess.CalledProcessError:
        pass

    # 생성
    print(f"[INFO] SQL 컨테이너({container_name}) 생성", file=sys.stderr)
    try:
        subprocess.run(
            [
                "az", "cosmosdb", "sql", "container", "create",
                "--account-name", account_name,
                "--resource-group", resource_group,
                "--database-name", database_name,
                "--name", container_name,
                "--partition-key-path", partition_key,
                "--throughput", str(throughput),
                "--output", "none",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] SQL 컨테이너 생성 실패: {e}", file=sys.stderr)
        sys.exit(1)


def set_cosmos_container_ttl(
    resource_group: str,
    account_name: str,
    database_name: str,
    container_name: str,
    ttl: int = -1,
) -> None:
    """
    컨테이너 TTL을 설정합니다.

    Args:
        resource_group: 리소스 그룹 이름
        account_name: 계정 이름
        database_name: 데이터베이스 이름
        container_name: 컨테이너 이름
        ttl: TTL 값 (-1 = 비활성화, 0 이상 = 초 단위)
    """
    print(f"[INFO] 컨테이너 {container_name} TTL 설정: {ttl}", file=sys.stderr)
    try:
        subprocess.run(
            [
                "az", "cosmosdb", "sql", "container", "update",
                "--account-name", account_name,
                "--resource-group", resource_group,
                "--database-name", database_name,
                "--name", container_name,
                "--ttl", str(ttl),
                "--output", "none",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] TTL 설정 실패: {e}", file=sys.stderr)
        sys.exit(1)


def set_cosmos_container_indexing_policy(
    resource_group: str,
    account_name: str,
    database_name: str,
    container_name: str,
    policy_file: str,
) -> None:
    """
    컨테이너 인덱싱 정책을 설정합니다.

    Args:
        resource_group: 리소스 그룹 이름
        account_name: 계정 이름
        database_name: 데이터베이스 이름
        container_name: 컨테이너 이름
        policy_file: 인덱싱 정책 JSON 파일 경로
    """
    print(f"[INFO] 컨테이너 {container_name} 인덱싱 정책 설정", file=sys.stderr)
    try:
        subprocess.run(
            [
                "az", "cosmosdb", "sql", "container", "update",
                "--account-name", account_name,
                "--resource-group", resource_group,
                "--database-name", database_name,
                "--name", container_name,
                "--idx", policy_file,
                "--output", "none",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] 인덱싱 정책 설정 실패: {e}", file=sys.stderr)
        sys.exit(1)


def get_cosmos_connection_string(
    resource_group: str,
    account_name: str,
) -> Optional[str]:
    """
    Cosmos DB 연결 문자열을 가져옵니다.

    Args:
        resource_group: 리소스 그룹 이름
        account_name: 계정 이름

    Returns:
        연결 문자열, 실패 시 None
    """
    try:
        result = subprocess.run(
            [
                "az", "cosmosdb", "keys", "list",
                "--name", account_name,
                "--resource-group", resource_group,
                "--type", "connection-strings",
                "--query", "connectionStrings[0].connectionString",
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
    print("=== azure/cosmos.py 테스트 ===", file=sys.stderr)
    print("이 모듈은 실제 Cosmos DB 계정이 필요하므로 테스트를 스킵합니다.", file=sys.stderr)
    print("함수 정의 확인:", file=sys.stderr)
    print("  create_cosmos_account_if_not_exists", file=sys.stderr)
    print("  create_cosmos_sql_database_if_not_exists", file=sys.stderr)
    print("  create_cosmos_sql_container_if_not_exists", file=sys.stderr)
    print("  set_cosmos_container_ttl", file=sys.stderr)
    print("  set_cosmos_container_indexing_policy", file=sys.stderr)
    print("  get_cosmos_connection_string", file=sys.stderr)
    print("테스트 완료.", file=sys.stderr)


if __name__ == "__main__":
    main()
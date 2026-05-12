#!/usr/bin/env python3
"""domain/resource_selector | parse RG names, list deployed RGs via az CLI, select from list | uses:domain/interactive,core/utils | parse_resource_group_name(),list_deployed_resource_groups(),select_from_list(),select_resource_group(),generate_names_from_rg()"""

import re
import subprocess
import sys
from typing import Dict, List, Optional

try:
    from .interactive import confirm_yn
except ImportError:
    # 독립 실행 시 대체
    from interactive import confirm_yn

try:
    from core.utils import log_info, log_warn, log_error
except ImportError:
    # 기본 로그 함수
    def log_info(message: str) -> None:
        print(f"[INFO] {message}", file=sys.stderr)
    def log_warn(message: str) -> None:
        print(f"[WARN] {message}", file=sys.stderr)
    def log_error(message: str) -> None:
        print(f"[ERROR] {message}", file=sys.stderr)


def parse_resource_group_name(rg: str) -> Dict[str, str]:
    """
    리소스 그룹 이름을 파싱합니다.

    형식: rg-{school_token}{nice_code}{school_level}{env}{deploy_num}-krc{region_code}
    예시: rg-kuhwa-sc-test-krc01

    Args:
        rg: 리소스 그룹 이름

    Returns:
        파싱된 구성 요소 딕셔너리:
            school_name_token, school_level, env_suffix, deploy_num, region_code
    """
    # 접두사 rg- 제거
    if not rg.startswith("rg-"):
        raise ValueError(f"리소스 그룹 이름이 'rg-'로 시작하지 않습니다: {rg}")
    stripped = rg[3:]  # "kuhwa-sc-test-krc01"

    # region_code 추출 (마지막 -krc 이후 두 자리 숫자)
    match = re.search(r"-krc(\d{2})$", stripped)
    if not match:
        raise ValueError(f"리소스 그룹 이름에 지역 코드가 없습니다: {rg}")
    region_code = match.group(1)
    # region_code 제거
    stripped_no_region = stripped[: -len(match.group(0))]  # "kuhwa-sc-test"

    # deploy_num은 region_code와 동일? 실제로는 deploy_num이 region_code 앞에 있을 수 있다.
    # 패턴에 따르면 deploy_num은 region_code 앞에 위치한다.
    # 여기서는 deploy_num을 region_code와 동일하게 간주 (리소스 그룹 이름 규칙에 따라)
    deploy_num = region_code

    # env_suffix 추출 (마지막 하이픈 이후)
    parts = stripped_no_region.split("-")
    if len(parts) < 3:
        raise ValueError(f"리소스 그룹 이름 형식이 잘못되었습니다: {rg}")
    env_suffix = parts[-1]  # "test"
    school_level = parts[-2]  # "sc"
    school_name_token = "-".join(parts[:-2])  # "kuhwa"

    return {
        "school_name_token": school_name_token,
        "school_level": school_level,
        "env_suffix": env_suffix,
        "deploy_num": deploy_num,
        "region_code": region_code,
    }


def list_deployed_resource_groups(pattern: Optional[str] = None) -> List[str]:
    """
    배포된 환경 리소스 그룹 목록을 가져옵니다.

    Args:
        pattern: 정규식 패턴 (기본값: rg-[a-z0-9]+-[a-z]+-[a-z]+-krc[0-9]{2})

    Returns:
        리소스 그룹 이름 목록
    """
    if pattern is None:
        pattern = r"^rg-[a-z0-9]+-[a-z]+-[a-z]+-krc[0-9]{2}$"

    try:
        cmd = [
            "az", "group", "list",
            "--query", "[?starts_with(name, 'rg-')].name",
            "-o", "tsv"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        output = result.stdout.strip()
        if not output:
            return []

        groups = []
        for line in output.splitlines():
            line = line.strip()
            if re.match(pattern, line):
                groups.append(line)
        groups.sort()
        return groups
    except subprocess.CalledProcessError as e:
        log_error(f"Azure CLI 조회 실패: {e}")
        sys.exit(1)
    except FileNotFoundError:
        log_error("Azure CLI가 설치되지 않았거나 PATH에 없습니다.")
        sys.exit(1)


def select_from_list(prompt: str, items: List[str]) -> str:
    """
    번호 목록에서 항목을 선택합니다.

    Args:
        prompt: 프롬프트 메시지
        items: 선택 가능한 항목 목록

    Returns:
        선택된 항목 문자열
    """
    if not items:
        raise ValueError("선택할 항목이 없습니다.")
    print("목록:", file=sys.stderr)
    for idx, item in enumerate(items, start=1):
        print(f"  {idx}. {item}", file=sys.stderr)
    while True:
        try:
            choice_input = input(prompt).strip()
        except EOFError:
            print("\n입력이 취소되었습니다.", file=sys.stderr)
            sys.exit(1)
        if not choice_input.isdigit():
            print("숫자를 입력해 주세요.", file=sys.stderr)
            continue
        choice = int(choice_input)
        if 1 <= choice <= len(items):
            return items[choice - 1]
        else:
            print(f"1~{len(items)} 사이의 번호를 입력해 주세요.", file=sys.stderr)


def select_resource_group(pattern: str) -> str:
    """
    리소스 그룹을 선택합니다 (대화형).

    Args:
        pattern: 리소스 그룹 필터 패턴

    Returns:
        선택된 리소스 그룹 이름
    """
    existing_rgs = list_deployed_resource_groups(pattern)
    if not existing_rgs:
        log_error("배포된 환경이 없습니다. 먼저 deploy-interactive.sh를 실행하세요.")
        sys.exit(1)

    print("배포된 환경 목록:", file=sys.stderr)
    return select_from_list("배포할 환경 번호 입력: ", existing_rgs)


def generate_names_from_rg(rg: str) -> Dict[str, str]:
    """
    리소스 그룹 이름에서 ACR 및 앱 이름을 생성합니다.

    Args:
        rg: 리소스 그룹 이름

    Returns:
        생성된 이름들을 포함한 딕셔너리
    """
    parsed = parse_resource_group_name(rg)
    token = parsed["school_name_token"]
    level = parsed["school_level"]
    env = parsed["env_suffix"]
    num = parsed["deploy_num"]
    region = parsed["region_code"]

    # ACR 이름 생성 (기존 naming.sh와 일관성 유지)
    # acr${SCHOOL_NAME_TOKEN}${SCHOOL_LEVEL}${ENV_SUFFIX}krc${DEPLOY_NUM}
    acr_name = f"acr{token}{level}{env}krc{num}"
    # 앱 이름 생성: app-${SCHOOL_NAME_TOKEN}-${SCHOOL_LEVEL}-${ENV_SUFFIX}-krc${DEPLOY_NUM}
    app_name = f"app-{token}-{level}-{env}-krc{num}"

    return {
        "selected_rg": rg,
        "acr_name": acr_name,
        "app_name": app_name,
        "school_name_token": token,
        "school_level": level,
        "env_suffix": env,
        "deploy_num": num,
        "region_code": region,
    }


def main() -> None:
    """독립 실행 테스트"""
    print("=== domain/resource_selector.py 테스트 ===", file=sys.stderr)
    # 가상 리소스 그룹 파싱 테스트
    test_rg = "rg-kuhwa-sc-test-krc01"
    print(f"테스트 리소스 그룹: {test_rg}", file=sys.stderr)
    parsed = parse_resource_group_name(test_rg)
    print(f"파싱 결과: {parsed}", file=sys.stderr)
    names = generate_names_from_rg(test_rg)
    print(f"생성된 이름: {names}", file=sys.stderr)
    # 목록 선택 테스트는 스킵
    print("테스트 완료.", file=sys.stderr)


if __name__ == "__main__":
    main()
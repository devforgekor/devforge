#!/usr/bin/env python3
"""domain/interactive | CLI prompts: y/n confirm, validated input, region codes, school level, deploy num | confirm_yn(),confirm_yes_default(),prompt_with_validation(),region_code_for_location(),region_display_name(),location_for_region_code(),select_school_level(),contains_deploy_num(),next_available_deploy_num()"""

import sys
import re
from typing import Callable, List, Optional

def confirm_yn(prompt: str) -> bool:
    """
    사용자에게 y/N 질문을 하고 결과를 반환합니다.

    Args:
        prompt: 프롬프트 메시지

    Returns:
        사용자가 'y' 또는 'Y'를 입력하면 True, 그렇지 않으면 False
    """
    try:
        answer = input(prompt).strip()
    except EOFError:
        return False
    return answer.lower() == 'y'

def confirm_yes_default(prompt: str) -> bool:
    """
    사용자에게 y/N 질문을 하고, 기본값을 yes로 합니다.
    (빈 입력도 yes로 간주)

    Args:
        prompt: 프롬프트 메시지

    Returns:
        사용자가 'y', 'Y' 또는 빈 문자열을 입력하면 True, 'n' 또는 'N'이면 False
    """
    try:
        answer = input(prompt).strip()
    except EOFError:
        return True  # 기본값 yes
    return answer.lower() in ('', 'y', 'yes')

def prompt_with_validation(prompt: str, validator: Callable[[str], bool], *args) -> str:
    """
    검증 함수를 통해 올바른 입력을 받을 때까지 반복합니다.

    Args:
        prompt: 프롬프트 메시지
        validator: 입력 문자열을 검증하는 함수 (성공 시 True 반환)
        *args: validator에 추가로 전달할 인자

    Returns:
        검증된 입력 문자열
    """
    while True:
        try:
            value = input(prompt).strip()
        except EOFError:
            print("\n입력이 취소되었습니다.", file=sys.stderr)
            sys.exit(1)
        if validator(value, *args):
            return value
        else:
            print("잘못된 입력입니다. 다시 입력해 주세요.", file=sys.stderr)

def region_code_for_location(location: str) -> str:
    """
    Azure 위치를 리전 약자로 변환합니다.

    Args:
        location: Azure 지역명 (예: koreacentral)

    Returns:
        리전 약자 (예: krc)

    Raises:
        ValueError: 지원하지 않는 리전인 경우
    """
    mapping = {
        "koreacentral": "krc",
        "centralindia": "cin",
        "eastasia": "eas",
        "japaneast": "jpe",
    }
    if location not in mapping:
        raise ValueError(f"지원하지 않는 리전입니다: {location}")
    return mapping[location]

def region_display_name(region_code: str) -> str:
    """
    리전 약자를 한글 표시명으로 변환합니다.

    Args:
        region_code: 리전 약자

    Returns:
        한글 표시명
    """
    mapping = {
        "krc": "한국 중부",
        "cin": "인도 중부",
        "jpe": "일본 동부",
        "eas": "동아시아",
    }
    return mapping.get(region_code, region_code)

def location_for_region_code(region_code: str) -> str:
    """
    리전 약자를 Azure 위치로 변환합니다.

    Args:
        region_code: 리전 약자

    Returns:
        Azure 지역명

    Raises:
        ValueError: 지원하지 않는 리전 약자인 경우
    """
    mapping = {
        "krc": "koreacentral",
        "cin": "centralindia",
        "jpe": "japaneast",
        "eas": "eastasia",
    }
    if region_code not in mapping:
        raise ValueError(f"지원하지 않는 리전 약자입니다: {region_code}")
    return mapping[region_code]

def select_school_level() -> str:
    """
    학교급을 선택합니다.

    Returns:
        학교급 코드 (kg/es/ms/hs/sc)
    """
    while True:
        print()
        print("1. 유치원: kg")
        print("2. 초등학교: es")
        print("3. 중학교: ms")
        print("4. 고등학교: hs")
        print("5. 특수 및 각종학교: sc")
        print("-----")
        try:
            choice = input("번호 입력 (1-5): ").strip()
        except EOFError:
            print("\n입력이 취소되었습니다.", file=sys.stderr)
            sys.exit(1)

        mapping = {
            "1": "kg",
            "2": "es",
            "3": "ms",
            "4": "hs",
            "5": "sc",
        }
        if choice in mapping:
            return mapping[choice]
        else:
            print("[WARN] 1~5 중에서 선택해 주세요.", file=sys.stderr)

def contains_deploy_num(target: str, used_nums: List[str]) -> bool:
    """
    대상 번호가 사용된 번호 목록에 포함되어 있는지 확인합니다.

    Args:
        target: 확인할 두 자리 번호 문자열
        used_nums: 사용된 번호 문자열 목록

    Returns:
        포함되어 있으면 True, 아니면 False
    """
    return target in used_nums

def next_available_deploy_num(used_nums: List[str]) -> str:
    """
    사용 가능한 다음 배포 번호를 찾습니다.

    Args:
        used_nums: 사용된 번호 문자열 목록

    Returns:
        사용 가능한 두 자리 번호 문자열 (01~99), 모두 사용 중이면 "99"
    """
    for i in range(1, 100):
        candidate = f"{i:02d}"
        if not contains_deploy_num(candidate, used_nums):
            return candidate
    return "99"  # fallback

if __name__ == "__main__":
    # 간단한 테스트
    print("=== domain/interactive.py 테스트 ===")
    print("confirm_yn 테스트 (y/N):")
    if confirm_yn("계속하시겠습니까? (y/N): "):
        print("YES 선택")
    else:
        print("NO 선택")
    print("\nregion_code_for_location 테스트:")
    print("koreacentral ->", region_code_for_location("koreacentral"))
    print("\nselect_school_level 테스트:")
    level = select_school_level()
    print(f"선택된 학교급: {level}")
    print("\n테스트 완료.")
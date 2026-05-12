#!/usr/bin/env python3
"""domain/deploy_selector | find used deploy nums via az CLI, resolve next available | uses:domain/interactive | get_used_deploy_nums(),resolve_deploy_num()"""

import subprocess
import sys
from typing import List

# 상대 임포트
try:
    from .interactive import next_available_deploy_num, contains_deploy_num
except ImportError:
    # 독립 실행 시 대체
    from interactive import next_available_deploy_num, contains_deploy_num


def get_used_deploy_nums(prefix: str, location: str) -> List[str]:
    """
    이미 사용된 deploy_num 목록을 Azure CLI로 조회합니다.

    Args:
        prefix: 리소스 그룹 이름 접두사 (예: 'rg-kuhwa-sc-test-krc')
        location: Azure 지역 (예: 'koreacentral')

    Returns:
        사용된 두 자리 번호 문자열 목록 (예: ['01', '02', '05'])
    """
    try:
        # az group list --query "[?location=='${location}' && starts_with(name, '${prefix}')].name" -o tsv
        cmd = [
            'az', 'group', 'list',
            '--query', f"[?location=='{location}' && starts_with(name, '{prefix}')].name",
            '-o', 'tsv'
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        output = result.stdout.strip()
        if not output:
            return []

        used = []
        for line in output.splitlines():
            line = line.strip()
            # 접두사 이후 두 자리 숫자 추출 (예: rg-kuhwa-sc-test-krc01 -> 01)
            if line.startswith(prefix):
                suffix = line[len(prefix):]
                if len(suffix) == 2 and suffix.isdigit():
                    used.append(suffix)
        # 중복 제거 및 정렬
        used = sorted(set(used))
        return used
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Azure CLI 조회 실패: {e}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print("[ERROR] Azure CLI가 설치되지 않았거나 PATH에 없습니다.", file=sys.stderr)
        sys.exit(1)


def resolve_deploy_num(prefix: str, location: str) -> str:
    """
    사용자에게 배포 번호를 결정받습니다.

    Args:
        prefix: 리소스 그룹 이름 접두사
        location: Azure 지역

    Returns:
        선택된 두 자리 배포 번호 문자열
    """
    used = get_used_deploy_nums(prefix, location)

    if used:
        print(f"기존 번호: {' '.join(used)}", file=sys.stderr)
    else:
        print("기존 배포 번호가 없습니다.", file=sys.stderr)

    recommended = next_available_deploy_num(used)
    try:
        input_num = input(f"사용할 deploy_num (추천: {recommended}): ").strip()
    except EOFError:
        print("\n입력이 취소되었습니다.", file=sys.stderr)
        sys.exit(1)

    if not input_num:
        return recommended

    # 입력값이 두 자리 숫자인지 확인
    if len(input_num) == 2 and input_num.isdigit():
        return input_num
    else:
        print(f"[WARN] 두 자리 숫자가 아니므로 추천 번호 '{recommended}'을 사용합니다.", file=sys.stderr)
        return recommended


def main() -> None:
    """독립 실행 테스트"""
    import sys
    print("=== domain/deploy_selector.py 테스트 ===", file=sys.stderr)
    # Azure CLI가 없을 경우를 대비한 모의 테스트
    try:
        subprocess.run(['az', '--version'], capture_output=True, check=True)
        print("Azure CLI가 설치되어 있습니다. 실제 쿼리 테스트 스킵.", file=sys.stderr)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Azure CLI가 없으므로 모의 데이터로 테스트합니다.", file=sys.stderr)
        # 모의 함수로 대체
        import sys
        sys.modules[__name__].get_used_deploy_nums = lambda prefix, location: ['01', '02', '05']

    # next_available_deploy_num 테스트
    used = ['01', '02', '05']
    next_num = next_available_deploy_num(used)
    print(f"사용된 번호: {used}, 다음 추천 번호: {next_num}", file=sys.stderr)
    # resolve_deploy_num 테스트 (비대화형)
    print("resolve_deploy_num 테스트는 사용자 입력이 필요하므로 스킵합니다.", file=sys.stderr)
    print("테스트 완료.", file=sys.stderr)


if __name__ == "__main__":
    main()
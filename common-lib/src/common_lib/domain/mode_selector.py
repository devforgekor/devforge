#!/usr/bin/env python3
"""domain/mode_selector | interactive deploy mode picker: test/prod, update/init | uses:domain/interactive | prompt_deployment_mode()"""

import sys
from typing import Dict, Any

try:
    from .interactive import confirm_yn
except ImportError:
    # 독립 실행 시 대체
    from interactive import confirm_yn


def prompt_deployment_mode() -> Dict[str, Any]:
    """
    사용자에게 배포 모드를 선택 요청합니다.

    Returns:
        {
            "mode": "update" 또는 "refresh",
            "allow_default_secrets": True 또는 False,
            "deploy_env": "test" 또는 "prod"
        }
    """
    menu = """
════════════════════════════════════════════════════════════
           Azure 인프라 배포 - 모드 선택
════════════════════════════════════════════════════════════

Test (기본값 적용)        Prod (실제값 필수)
  1. 업데이트             11. 업데이트
  2. 초기화 [주의]         22. 초기화 [주의]

  3. 종료
"""
    print(menu, file=sys.stderr)

    while True:
        try:
            choice = input("선택 (1/2/11/22/3): ").strip()
        except EOFError:
            print("\n입력이 취소되었습니다.", file=sys.stderr)
            sys.exit(1)

        if choice == "3":
            print("종료합니다.", file=sys.stderr)
            sys.exit(0)

        mapping = {
            "1": ("update", True, "test"),
            "2": ("refresh", True, "test"),
            "11": ("update", False, "prod"),
            "22": ("refresh", False, "prod"),
        }
        if choice in mapping:
            mode, allow_default_secrets, deploy_env = mapping[choice]
            break
        else:
            print("", file=sys.stderr)
            print("값을 잘못 입력하였습니다. 재 실행해 주세요.", file=sys.stderr)
            sys.exit(1)

    # 초기화 재확인
    if mode == "refresh":
        print("", file=sys.stderr)
        if not confirm_yn("[주의] 초기화 후 복구 불가능합니다. 진행? (y/N): "):
            print("취소되었습니다. 재 실행해 주세요.", file=sys.stderr)
            sys.exit(0)

    return {
        "mode": mode,
        "allow_default_secrets": allow_default_secrets,
        "deploy_env": deploy_env,
    }


def main() -> None:
    """독립 실행 테스트"""
    print("=== domain/mode_selector.py 테스트 ===", file=sys.stderr)
    print("대화형 테스트를 시작합니다.", file=sys.stderr)
    result = prompt_deployment_mode()
    print(
        f"MODE={result['mode']}, "
        f"ALLOW_DEFAULT_SECRETS={result['allow_default_secrets']}, "
        f"DEPLOY_ENV={result['deploy_env']}",
        file=sys.stderr
    )
    print("테스트 완료.", file=sys.stderr)


if __name__ == "__main__":
    main()
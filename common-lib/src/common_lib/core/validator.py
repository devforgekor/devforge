#!/usr/bin/env python3
"""core/validator | validate tokens, NICE codes, deploy nums, Korean names, prod secrets | validate_school_token(),validate_nice_code(),validate_deploy_num(),validate_base_name_length(),validate_production_secrets(),validate_korean_name()"""
import os
import re
import sys

def validate_school_token(token: str) -> bool:
    if re.fullmatch(r'^[a-z0-9]{2,10}$', token):
        return True
    else:
        print("[ERROR] school_name_token은 영문 소문자와 숫자로 2~10자여야 합니다.", file=sys.stderr)
        return False

def validate_nice_code(code: str) -> bool:
    """
    NICE 학교 코드가 소문자와 숫자로 10자리인지 검증합니다.

    매개변수:
        code: 검증할 NICE 코드 문자열

    반환값:
        유효하면 True, 그렇지 않으면 False
    """
    if re.fullmatch(r'^[a-z0-9]{10}$', code):
        return True
    else:
        print("[ERROR] NICE 코드는 영문 소문자와 숫자로 10자리여야 합니다.", file=sys.stderr)
        return False

def validate_deploy_num(num: str) -> bool:
    """
    배포 번호가 두 자리 숫자인지 검증합니다.

    매개변수:
        num: 검증할 배포 번호 문자열

    반환값:
        유효하면 True, 그렇지 않으면 False
    """
    if re.fullmatch(r'^[0-9]{2}$', num):
        return True
    else:
        print("[ERROR] deploy_num은 두 자리 숫자여야 합니다.", file=sys.stderr)
        return False

def validate_base_name_length(base_name: str) -> bool:
    """
    기본 이름이 22자를 초과하지 않는지 검증합니다.

    매개변수:
        base_name: 검증할 기본 이름 문자열

    반환값:
        유효하면 True, 그렇지 않으면 False
    """
    if len(base_name) > 22:
        print(f"[ERROR] 기본 이름이 너무 깁니다: '{base_name}' ({len(base_name)}자, 최대 22자)", file=sys.stderr)
        return False
    return True

def validate_production_secrets() -> None:
    """
    프로덕션 모드에서 필수 환경 변수가 설정되었는지 확인합니다.
    ALLOW_DEFAULT_SECRETS가 false일 때만 검사를 수행합니다.
    """
    if os.getenv("ALLOW_DEFAULT_SECRETS", "true").lower() == "false":
        print("\n프로덕션 환경 필수 환경변수 확인 중...")
        missing_vars = []
        required = [
            "AZURE_OPENAI_ENDPOINT",
            "API_KEY_AI",
            "API_KEY_SMS",
            "API_SECRET_SMS",
            "SMS_SENDER_PHONE",
        ]
        for var in required:
            if not os.getenv(var):
                missing_vars.append(var)
        if missing_vars:
            print("\n오류: 다음 환경변수가 설정되지 않았습니다.")
            for var in missing_vars:
                print(f"  - {var}")
            print("\n사용법:")
            print("  export AZURE_OPENAI_ENDPOINT=\"https://...openai.azure.com/\"")
            print("  export API_KEY_AI=\"your-api-key\"")
            print("  export API_KEY_SMS=\"your-sms-key\"")
            print("  export API_SECRET_SMS=\"your-sms-secret\"")
            print("  export SMS_SENDER_PHONE=\"01012345678\"")
            sys.exit(1)
        print("프로덕션 환경 필수값 확인 완료")

def validate_korean_name(name: str) -> bool:
    """
    학교명이 한글만 포함되어 있는지 검증합니다.

    매개변수:
        name: 검증할 학교명 문자열

    반환값:
        유효하면 True, 그렇지 않으면 False
    """
    import re
    if re.fullmatch(r'^[가-힣\s]+$', name):
        return True
    else:
        print("[ERROR] 학교명은 한글만 입력해야 합니다.", file=sys.stderr)
        return False

def main():
    import argparse
    import sys
    parser = argparse.ArgumentParser(description="입력값 검증 유틸리티")
    subparsers = parser.add_subparsers(dest="command", help="검증 명령")

    # validate_school_token
    parser_token = subparsers.add_parser("validate_school_token", help="학교 토큰 검증")
    parser_token.add_argument("token", help="학교 토큰 문자열")

    # validate_deploy_num
    parser_num = subparsers.add_parser("validate_deploy_num", help="배포 번호 검증")
    parser_num.add_argument("num", help="배포 번호 문자열")

    # validate_base_name_length
    parser_length = subparsers.add_parser("validate_base_name_length", help="기본 이름 길이 검증")
    parser_length.add_argument("base_name", help="기본 이름 문자열")

    # validate_nice_code
    parser_nice = subparsers.add_parser("validate_nice_code", help="NICE 코드 검증")
    parser_nice.add_argument("code", help="NICE 코드 문자열")

    # validate_production_secrets
    subparsers.add_parser("validate_production_secrets", help="프로덕션 환경변수 검증")

    # validate_korean_name
    parser_korean = subparsers.add_parser("validate_korean_name", help="학교명 한글 검증")
    parser_korean.add_argument("name", help="학교명 문자열")

    args = parser.parse_args()

    if args.command == "validate_school_token":
        if validate_school_token(args.token):
            sys.exit(0)
        else:
            sys.exit(1)
    elif args.command == "validate_deploy_num":
        if validate_deploy_num(args.num):
            sys.exit(0)
        else:
            sys.exit(1)
    elif args.command == "validate_base_name_length":
        if validate_base_name_length(args.base_name):
            sys.exit(0)
        else:
            sys.exit(1)
    elif args.command == "validate_production_secrets":
        validate_production_secrets()
        sys.exit(0)
    elif args.command == "validate_nice_code":
        if validate_nice_code(args.code):
            sys.exit(0)
        else:
            sys.exit(1)
    elif args.command == "validate_korean_name":
        if validate_korean_name(args.name):
            sys.exit(0)
        else:
            sys.exit(1)
    else:
        # 기본 테스트 실행
        print("=== core/validator.py 테스트 ===")
        if validate_school_token("test123"):
            print("✓ validate_school_token 성공")
        else:
            print("✗ validate_school_token 실패")
        if validate_school_token("TEST"):
            print("✗ validate_school_token 실패 예상했으나 성공")
        else:
            print("✓ validate_school_token 실패 적절히 처리")
        if validate_deploy_num("12"):
            print("✓ validate_deploy_num 성공")
        else:
            print("✗ validate_deploy_num 실패")
        if validate_base_name_length("short"):
            print("✓ validate_base_name_length 성공")
        else:
            print("✗ validate_base_name_length 실패")
        if validate_korean_name("서울초등학교"):
            print("✓ validate_korean_name 성공")
        else:
            print("✗ validate_korean_name 실패")
        if validate_korean_name("서울abc학교"):
            print("✗ validate_korean_name 실패 예상했으나 성공")
        else:
            print("✓ validate_korean_name 실패 적절히 처리")
        print("테스트 완료.")

if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""core/naming | Azure resource name generator: rg/st/kv/app/acr/cosmos/func | generate_resource_names(),generate_shared_names(),generate_legacy_school_names()"""
import json
import re
import sys
from typing import Dict, Tuple

def generate_resource_names(
    school_token: str,
    nice: str,
    level: str,
    env: str,
    num: str
) -> Dict[str, str]:
    base_name = f"{school_token}{nice}{level}{env}{num}"
    safe_base = base_name.replace("-", "")
    names = {
        "BASE_NAME": base_name,
        "SAFE_BASE": safe_base,
        "RESOURCE_GROUP": f"rg-{base_name}",
        "STORAGE_ACCOUNT": f"st{safe_base}",
        "KEYVAULT": f"kv-{base_name}",
        "CONTAINER_APP": f"app-{base_name}",
        "FUNCTION_APP": f"func-{base_name}",
        "ACR": f"acr{safe_base}",
        "LOG_ANALYTICS": f"law-{base_name}",
        "IDENTITY": f"id-{base_name}",
    }
    return names

def generate_shared_names(
    token: str,
    nice: str,
    level: str,
    region: str
) -> Dict[str, str]:
    """
    공유 인프라 리소스 이름을 생성합니다.

    매개변수:
        token: 학교 토큰
        nice: 교육청 코드
        level: 학교급 코드
        region: 지역 코드 (예: krc)

    반환값:
        공유 리소스 이름 딕셔너리
    """
    suffix = f"{token}{nice}{level}"
    names = {
        "SHARED_RG": f"rg-shared-infra-{suffix}-{region}00",
        "SHARED_COSMOS": f"cosmos-ssot-{suffix}-{region}00",
        "SHARED_ENV": f"env-ssot-{suffix}-{region}00",
    }
    return names

def generate_legacy_school_names(
    env: str,
    loc: str
) -> Dict[str, str]:
    """
    레거시 학교 배포용 고정 리소스명을 생성합니다.

    매개변수:
        env: 환경 (test/prod)
        loc: 위치 코드 (예: krc)

    반환값:
        레거시 리소스 이름 딕셔너리
    """
    suffix = f"school-{env}-{loc}"
    safe_suffix = suffix.replace("-", "")
    names = {
        "LEGACY_RG": f"rg-{suffix}",
        "LEGACY_KV": f"kv-{suffix}01",
        "LEGACY_STORAGE": f"st{safe_suffix}02",
        "LEGACY_COSMOS": f"cosmos-{suffix}01",
        "LEGACY_ACR": f"acr{safe_suffix}01",
        "LEGACY_ENV_NAME": f"env-{suffix}01",
        "LEGACY_APP": f"app-{suffix}01",
    }
    return names

def main():
    import argparse
    parser = argparse.ArgumentParser(description="리소스명 생성 유틸리티")
    subparsers = parser.add_subparsers(dest="command", help="생성 명령")

    # generate_resource_names
    parser_resource = subparsers.add_parser("resource", help="주요 리소스 이름 생성")
    parser_resource.add_argument("school_token", help="학교 토큰")
    parser_resource.add_argument("nice", help="교육청 코드")
    parser_resource.add_argument("level", help="학교급 코드")
    parser_resource.add_argument("env", help="환경 코드 (t/p)")
    parser_resource.add_argument("num", help="배포 번호 (두 자리)")
    parser_resource.add_argument("--json", action="store_true", help="JSON 출력")

    # generate_shared_names
    parser_shared = subparsers.add_parser("shared", help="공유 인프라 이름 생성")
    parser_shared.add_argument("token", help="학교 토큰")
    parser_shared.add_argument("nice", help="교육청 코드")
    parser_shared.add_argument("level", help="학교급 코드")
    parser_shared.add_argument("region", help="지역 코드")
    parser_shared.add_argument("--json", action="store_true", help="JSON 출력")

    # generate_legacy_school_names
    parser_legacy = subparsers.add_parser("legacy", help="레거시 리소스 이름 생성")
    parser_legacy.add_argument("env", help="환경 (test/prod)")
    parser_legacy.add_argument("loc", help="위치 코드")
    parser_legacy.add_argument("--json", action="store_true", help="JSON 출력")

    args = parser.parse_args()

    if args.command == "resource":
        result = generate_resource_names(args.school_token, args.nice, args.level, args.env, args.num)
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            for key, value in result.items():
                print(f"{key}: {value}")
    elif args.command == "shared":
        result = generate_shared_names(args.token, args.nice, args.level, args.region)
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            for key, value in result.items():
                print(f"{key}: {value}")
    elif args.command == "legacy":
        result = generate_legacy_school_names(args.env, args.loc)
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            for key, value in result.items():
                print(f"{key}: {value}")
    else:
        # 기본 테스트 실행
        print("=== core/naming.py 테스트 ===")
        result = generate_resource_names("test", "sen", "hs", "t", "01")
        for key, value in result.items():
            print(f"{key}: {value}")
        shared = generate_shared_names("test", "sen", "hs", "krc")
        for key, value in shared.items():
            print(f"{key}: {value}")
        legacy = generate_legacy_school_names("test", "krc")
        for key, value in legacy.items():
            print(f"{key}: {value}")
        print("테스트 완료.")

if __name__ == "__main__":
    main()
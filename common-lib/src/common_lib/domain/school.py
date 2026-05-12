#!/usr/bin/env python3
"""domain/school | interactive school info input, JSON save/load, 17 NICE office codes | uses:domain/interactive | select_nice_code(),school_info_file(),input_school_info(),save_school_info(),load_school_info()"""

import json
import os
import sys
from pathlib import Path
from typing import Dict, Optional

try:
    from .interactive import select_school_level, prompt_with_validation
except ImportError:
    # 독립 실행 시 대체
    from interactive import select_school_level, prompt_with_validation

def select_nice_code() -> str:
    """
    교육청 코드를 선택합니다.

    Returns:
        선택된 교육청 코드 (예: "sen", "goe")
    """
    print("교육청을 선택하세요 (번호 입력):")
    print("1) 서울(SEN)    4) 인천(ICE)   7) 대전(DJE)  10) 강원(KWE)  13) 전북(JBE)  16) 경남(GNE)")
    print("2) 경기(GOE)    5) 대구(DGE)   8) 울산(USE)  11) 충북(CBE)  14) 전남(JNE)  17) 제주(JJE)")
    print("3) 부산(PEN)    6) 광주(GJE)   9) 세종(SJE)  12) 충남(CNE)  15) 경북(GBE)")

    while True:
        try:
            choice = input("번호 입력: ").strip()
        except EOFError:
            print("\n입력이 취소되었습니다.", file=sys.stderr)
            sys.exit(1)

        mapping = {
            "1": "sen", "2": "goe", "3": "pen", "4": "ice", "5": "dge",
            "6": "gje", "7": "dje", "8": "use", "9": "sje", "10": "kwe",
            "11": "cbe", "12": "cne", "13": "jbe", "14": "jne", "15": "gbe",
            "16": "gne", "17": "jje"
        }
        if choice in mapping:
            return mapping[choice]
        else:
            print("잘못된 선택, 다시 입력하세요.", file=sys.stderr)

def school_info_file(token: str, nice: str, level: str, env: str) -> str:
    """
    학교 정보 파일의 저장 경로를 생성합니다.

    Args:
        token: 학교 토큰
        nice: 교육청 코드
        level: 학교급 코드
        env: 환경 코드 (t=테스트, p=운영)

    Returns:
        파일 경로 문자열
    """
    return f"schools/{token}{nice}{level}{env}.json"

def input_school_info() -> Dict[str, str]:
    """
    학교 정보를 대화형으로 입력받습니다.

    Returns:
        학교 정보를 담은 딕셔너리
    """
    # validator 모듈 동적 임포트
    try:
        from core.validator import validate_nice_code, validate_korean_name, validate_school_token
    except ImportError:
        print("[WARN] validator 모듈을 찾을 수 없어 간단한 검증으로 대체합니다.", file=sys.stderr)
        # 임시 검증 함수
        def validate_nice_code(code: str) -> bool:
            return len(code) == 10 and code.isalnum()
        def validate_korean_name(name: str) -> bool:
            return all('\uac00' <= ch <= '\ud7a3' for ch in name)
        def validate_school_token(token: str) -> bool:
            return 2 <= len(token) <= 10 and token.isalnum() and token.islower()


    # 나이스 학교코드
    school_nice_code = prompt_with_validation(
        "나이스 학교코드 (예: b100000123): ",
        validate_nice_code
    )
    # 학교명(한글)
    school_korean_name = prompt_with_validation(
        "학교명(한글): ",
        validate_korean_name
    )
    # 학교 토큰
    school_name_token = prompt_with_validation(
        "school_name_token (영문소문자/숫자 2~10자): ",
        validate_school_token
    )
    # 교육청 코드 선택
    nice_code = select_nice_code()
    # 학교레벨 선택 (임시 구현)
    school_level = select_school_level()

    return {
        "school_nice_code": school_nice_code,
        "school_korean_name": school_korean_name,
        "school_name_token": school_name_token,
        "nice_code": nice_code,
        "school_level": school_level
    }


def save_school_info(info: Dict[str, str], out_file: str) -> None:
    """
    학교 정보를 JSON 파일로 저장합니다.

    Args:
        info: 학교 정보 딕셔너리
        out_file: 출력 파일 경로
    """
    out_path = Path(out_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(info, f, ensure_ascii=False, indent=2)

def load_school_info(in_file: str) -> Optional[Dict[str, str]]:
    """
    JSON 파일에서 학교 정보를 로드합니다.

    Args:
        in_file: 입력 파일 경로

    Returns:
        학교 정보 딕셔너리 (파일이 없거나 형식이 잘못된 경우 None)
    """
    in_path = Path(in_file)
    if not in_path.is_file():
        print(f"학교 정보 파일이 없습니다: {in_file}", file=sys.stderr)
        return None
    try:
        with open(in_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            # 필수 키 확인
            required_keys = {"school_nice_code", "school_korean_name", "school_name_token", "nice_code", "school_level"}
            if not all(key in data for key in required_keys):
                print(f"파일 형식이 잘못되었습니다: {in_file}", file=sys.stderr)
                return None
            return data
    except json.JSONDecodeError:
        print(f"JSON 파싱 오류: {in_file}", file=sys.stderr)
        return None

if __name__ == "__main__":
    # 독립 실행 테스트
    print("=== domain/school.py 테스트 ===")
    print("교육청 코드 선택 테스트:")
    code = select_nice_code()
    print(f"선택된 코드: {code}")
    print("\n학교급 선택 테스트:")
    level = select_school_level()
    print(f"선택된 학교급: {level}")
    print("\n파일 경로 생성 테스트:")
    path = school_info_file("test", "sen", "hs", "t")
    print(f"경로: {path}")
    # 입력 테스트는 생략
    print("테스트 완료.")
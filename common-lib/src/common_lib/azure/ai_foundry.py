#!/usr/bin/env python3
"""azure/ai_foundry | Azure AI Foundry: create OpenAI account, deploy model, get keys | create_openai_account(),deploy_openai_model(),get_openai_keys()"""

import subprocess
import sys
import json

def create_openai_account(account_name: str, resource_group: str, location: str, sku: str = "S0") -> bool:
    """
    OpenAI 계정을 생성합니다 (Cognitive Services).
    
    매개변수:
        account_name: 계정 이름
        resource_group: 리소스 그룹 이름
        location: Azure 지역
        sku: SKU (기본값 "S0")
    
    반환값:
        성공 시 True, 실패 시 False
    """
    print(f"[INFO] OpenAI 계정 생성: {account_name} (리소스 그룹: {resource_group})", file=sys.stderr)
    
    # 계정 존재 여부 확인
    try:
        subprocess.run(
            ["az", "cognitiveservices", "account", "show",
             "--name", account_name, "--resource-group", resource_group],
            capture_output=True,
            check=True,
            text=True,
            encoding="utf-8"
        )
        print("[INFO] OpenAI 계정이 이미 존재합니다.", file=sys.stderr)
        return True
    except subprocess.CalledProcessError:
        pass  # 존재하지 않음
    
    # 계정 생성
    try:
        subprocess.run(
            ["az", "cognitiveservices", "account", "create",
             "--name", account_name,
             "--resource-group", resource_group,
             "--location", location,
             "--kind", "OpenAI",
             "--sku", sku,
             "--custom-domain", account_name,
             "--only-show-errors",
             "--output", "none"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8"
        )
        print(f"[SUCCESS] OpenAI 계정 생성 완료: {account_name}", file=sys.stderr)
        return True
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] OpenAI 계정 생성 실패: {e.stderr}", file=sys.stderr)
        return False

def deploy_openai_model(account_name: str, resource_group: str,
                        model_name: str = "gpt-35-turbo", model_version: str = "0613",
                        capacity: int = 10) -> bool:
    """
    OpenAI 모델을 배포합니다.
    
    매개변수:
        account_name: OpenAI 계정 이름
        resource_group: 리소스 그룹 이름
        model_name: 모델 이름 (기본값 "gpt-35-turbo")
        model_version: 모델 버전 (기본값 "0613")
        capacity: 용량 (기본값 10)
    
    반환값:
        성공 시 True, 실패 시 False
    """
    print(f"[INFO] OpenAI 모델 배포: {model_name} (계정: {account_name})", file=sys.stderr)
    
    # 배포 이름 생성 (점을 하이픈으로 대체)
    deployment_name = model_name.replace(".", "-")
    
    # 모델 배포 상태 확인
    try:
        subprocess.run(
            ["az", "cognitiveservices", "account", "deployment", "show",
             "--name", account_name,
             "--resource-group", resource_group,
             "--deployment-name", deployment_name],
            capture_output=True,
            check=True,
            text=True,
            encoding="utf-8"
        )
        print("[INFO] 모델이 이미 배포되어 있습니다.", file=sys.stderr)
        return True
    except subprocess.CalledProcessError:
        pass  # 존재하지 않음
    
    # 모델 배포
    try:
        subprocess.run(
            ["az", "cognitiveservices", "account", "deployment", "create",
             "--name", account_name,
             "--resource-group", resource_group,
             "--deployment-name", deployment_name,
             "--model-name", model_name,
             "--model-version", model_version,
             "--model-format", "OpenAI",
             "--scale-settings-scale-type", "Standard",
             "--scale-settings-capacity", str(capacity),
             "--only-show-errors",
             "--output", "none"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8"
        )
        print(f"[SUCCESS] OpenAI 모델 배포 완료: {model_name}", file=sys.stderr)
        return True
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] 모델 배포 실패: {e.stderr}", file=sys.stderr)
        return False

def get_openai_keys(account_name: str, resource_group: str) -> dict:
    """
    OpenAI 계정 키를 조회합니다.
    
    매개변수:
        account_name: 계정 이름
        resource_group: 리소스 그룹 이름
    
    반환값:
        키가 포함된 딕셔너리 (예: {"key1": "...", "key2": "..."})
    """
    print(f"[INFO] OpenAI 키 조회: {account_name}", file=sys.stderr)
    try:
        result = subprocess.run(
            ["az", "cognitiveservices", "account", "keys", "list",
             "--name", account_name,
             "--resource-group", resource_group,
             "--query", "{key1: key1, key2: key2}",
             "-o", "json"],
            capture_output=True,
            check=True,
            text=True,
            encoding="utf-8"
        )
        return json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
        print(f"[ERROR] 키 조회 실패: {e}", file=sys.stderr)
        return {}

if __name__ == "__main__":
    # 독립 실행 테스트 블록
    print("=== azure/ai_foundry.py 테스트 ===", file=sys.stderr)
    print("이 모듈은 실제 Azure AI 리소스를 생성하므로 테스트를 스킵합니다.", file=sys.stderr)
    print("함수 정의 확인:", file=sys.stderr)
    print(f"  create_openai_account: {create_openai_account.__doc__.splitlines()[0]}")
    print(f"  deploy_openai_model: {deploy_openai_model.__doc__.splitlines()[0]}")
    print(f"  get_openai_keys: {get_openai_keys.__doc__.splitlines()[0]}")
    print("테스트 완료.", file=sys.stderr)
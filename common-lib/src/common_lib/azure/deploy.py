#!/usr/bin/env python3
"""azure/deploy | Bicep build -> az deployment sub create -> poll provisioning state | bicep_build(),run_whatif(),run_deployment()"""

import subprocess
import sys
import os
import tempfile
import json
import time
from typing import List, Optional

def bicep_build(template_file: str) -> bool:
    """
    Bicep 파일을 JSON으로 빌드합니다.
    
    매개변수:
        template_file: Bicep 템플릿 파일 경로
    
    반환값:
        성공 시 True, 실패 시 False
    """
    tmp_json = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp_json.close()
    try:
        subprocess.run(
            ["az", "bicep", "build", "--file", template_file, "--outfile", tmp_json.name],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8"
        )
        os.unlink(tmp_json.name)
        return True
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Bicep 빌드 실패: {e.stderr}", file=sys.stderr)
        if os.path.exists(tmp_json.name):
            os.unlink(tmp_json.name)
        return False

def run_whatif(deploy_name: str, template_file: str, params: List[str], location: Optional[str] = None):
    """
    what-if 실행 및 결과를 JSON 파일로 저장합니다.
    
    매개변수:
        deploy_name: 배포 이름
        template_file: Bicep 템플릿 파일 경로
        params: 추가 파라미터 리스트 (예: ["param1=value1", "param2=value2"])
        location: Azure 지역 (환경 변수 LOCATION 사용)
    """
    if location is None:
        location = os.getenv("LOCATION", "koreacentral")
    
    output_file = f"logs/{deploy_name}-whatif.json"
    os.makedirs("logs", exist_ok=True)
    
    cmd = ["az", "deployment", "sub", "what-if",
           "--location", location,
           "--template-file", template_file,
           "--no-pretty-print", "-o", "json",
           "--parameters"]
    cmd.extend(params)
    
    try:
        with open(output_file, "w", encoding="utf-8") as f:
            subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, check=True, text=True, encoding="utf-8")
        print(f"[INFO] what-if 결과 저장됨: {output_file}", file=sys.stderr)
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] what-if 실행 실패: {e.stderr}", file=sys.stderr)
        sys.exit(1)

def run_deployment(deploy_name: str, template_file: str, params: List[str], location: Optional[str] = None) -> bool:
    """
    배포를 생성하고 상태를 모니터링합니다.
    
    매개변수:
        deploy_name: 배포 이름
        template_file: Bicep 템플릿 파일 경로
        params: 추가 파라미터 리스트
        location: Azure 지역 (환경 변수 LOCATION 사용)
    
    반환값:
        성공 시 True, 실패 시 False
    """
    if location is None:
        location = os.getenv("LOCATION", "koreacentral")
    
    print(f"[INFO] 배포 시작: {deploy_name}", file=sys.stderr)
    
    cmd = ["az", "deployment", "sub", "create",
           "--name", deploy_name,
           "--location", location,
           "--template-file", template_file,
           "--output", "none",
           "--parameters"]
    cmd.extend(params)
    
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, encoding="utf-8")
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] 배포 생성 실패: {e.stderr}", file=sys.stderr)
        return False
    
    # 상태 확인 루프
    state = "Running"
    while state == "Running":
        time.sleep(10)
        try:
            result = subprocess.run(
                ["az", "deployment", "sub", "show", "--name", deploy_name,
                 "--query", "properties.provisioningState", "-o", "tsv"],
                capture_output=True,
                text=True,
                encoding="utf-8"
            )
            state = result.stdout.strip() if result.stdout else "Unknown"
        except subprocess.CalledProcessError:
            state = "Unknown"
        print(f"[INFO] 배포 상태: {state}", file=sys.stderr)
    
    if state != "Succeeded":
        print("[ERROR] 배포 실패. 상세 내용:", file=sys.stderr)
        subprocess.run(
            ["az", "deployment", "sub", "show", "--name", deploy_name,
             "--query", "properties.error", "-o", "yaml"],
            stderr=subprocess.STDOUT
        )
        return False
    return True

if __name__ == "__main__":
    # 독립 실행 테스트 블록
    print("=== azure/deploy.py 테스트 ===", file=sys.stderr)
    print("이 모듈은 실제 Azure 배포를 수행하므로 테스트를 스킵합니다.", file=sys.stderr)
    print("함수 정의 확인:", file=sys.stderr)
    print(f"  bicep_build: {bicep_build.__doc__.splitlines()[0]}")
    print(f"  run_whatif: {run_whatif.__doc__.splitlines()[0]}")
    print(f"  run_deployment: {run_deployment.__doc__.splitlines()[0]}")
    print("테스트 완료.", file=sys.stderr)
#!/usr/bin/env python3
"""azure/test | post-deploy HTTP smoke test: status check, Container App FQDN probe | check_http_status(),smoke_test_container_app()"""

import sys
import urllib.request
import urllib.error
import ssl
import json
from typing import Optional, Dict, Any


def check_http_status(
    url: str,
    expected_status: int,
    timeout: int = 15,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    data: Optional[str] = None,
) -> bool:
    """
    HTTP 엔드포인트 상태 코드를 확인합니다.

    Args:
        url: 대상 URL
        expected_status: 예상 HTTP 상태 코드
        timeout: 타임아웃 (초, 기본값 15)
        method: HTTP 메서드 (기본값 "GET")
        headers: 추가 헤더 (선택)
        data: 요청 본문 (선택)

    Returns:
        예상 상태 코드와 일치하면 True, 그렇지 않으면 False
    """
    req_headers = {}
    if headers:
        req_headers.update(headers)
    if data and "Content-Type" not in req_headers:
        req_headers["Content-Type"] = "application/json"

    # 요청 객체 생성
    request = urllib.request.Request(url, method=method)
    for key, value in req_headers.items():
        request.add_header(key, value)

    if data is not None:
        request.data = data.encode("utf-8")

    # SSL 컨텍스트 생성 (인증서 검증 무시하지 않음)
    context = ssl.create_default_context()

    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            status_code = response.status
            if status_code == expected_status:
                print(f"  {method} {url} → {status_code} OK ✓", file=sys.stderr)
                return True
            else:
                print(f"  {method} {url} → [WARN] HTTP {status_code} ({expected_status} 예상)", file=sys.stderr)
                return False
    except urllib.error.HTTPError as e:
        # HTTPError는 응답이 존재하므로 상태 코드 사용
        status_code = e.code
        print(f"  {method} {url} → [WARN] HTTP {status_code} ({expected_status} 예상)", file=sys.stderr)
        return False
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        # 네트워크 오류
        print(f"  {method} {url} → [ERROR] 연결 실패: {e}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"  {method} {url} → [ERROR] 예외 발생: {e}", file=sys.stderr)
        return False


def smoke_test_container_app(fqdn: str) -> None:
    """
    컨테이너 앱 스모크 테스트 (SchoolDocs 특화)

    Args:
        fqdn: 컨테이너 앱의 FQDN (예: myapp.azurecontainer.io)
    """
    print(f"[INFO] 스모크 테스트: {fqdn}", file=sys.stderr)

    status_url = f"https://{fqdn}/api/status"
    pending_url = f"https://{fqdn}/api/manage/pending"

    # POST /api/status → 200
    check_http_status(
        status_url,
        expected_status=200,
        timeout=15,
        method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps({"name": "홍길동", "birthdate": "1990-01-01"}),
    )

    # GET /api/manage/pending → 401 (인증 필요)
    check_http_status(
        pending_url,
        expected_status=401,
        timeout=15,
        method="GET",
    )


def main() -> None:
    """독립 실행 테스트"""
    print("=== azure/test.py 테스트 ===", file=sys.stderr)
    print("실제 HTTP 엔드포인트가 없으므로 테스트 스킵", file=sys.stderr)
    print("함수 정의 확인:", file=sys.stderr)
    print(f"  check_http_status: {check_http_status}", file=sys.stderr)
    print(f"  smoke_test_container_app: {smoke_test_container_app}", file=sys.stderr)
    print("테스트 완료.", file=sys.stderr)


if __name__ == "__main__":
    main()
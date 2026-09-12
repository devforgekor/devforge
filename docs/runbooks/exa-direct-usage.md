# 런북 — EXA 직접 사용 (MCP 비활성화)
# > Status: active · Date: 2026-09-12 · Owner: devforge · Related: `specs/exa-direct-usage.yaml`, `reports/mcp-consolidation-applied-20260911.md`
# EXA MCP를 비활성화하고 opencode 세션에서 Python 직접 import 방식으로 사용

---

## 0. 트리거 (언제)

- opencode 세션에서 웹 검색/리서치가 필요할 때
- MCP 도구 목록이 너무 많아져서 세션 속도가 저하될 때
- EXA API를 직접 호출해 빠르게 결과를 얻고 싶을 때

---

## 1. 현황

- **MCP 상태**: `exa-search` 비활성화 (`enabled: false`)
- **대체 방식**: `lib.research.exa` 직접 import
- **API 키**: `~/.config/devforge/secrets.env`의 `EXA_API_KEYS` (4개, 복호화됨)
- **키 순환**: 429(rate limit) 발생 시 round-robin으로 다음 키 사용
- **rr proxy**: EXA와 무관 (LLM API 전용). EXA는 `https://api.exa.ai`로 직접 호출

### 왜 MCP를 비활성화했나

| 항목 | MCP | 직접 import |
|---|---|---|
| 속도 | 느림 (도구 등록/호출 오버헤드) | 빠름 |
| 도구 수 | 많음 | 필요 없음 |
| 유연성 | 고정 스키마 | 자유 파라미터 |
| 복잡도 | 별도 서버 필요 | import만 |

---

## 2. 선행조건

1. **EXA API 키 확인**
   ```bash
   grep "EXA_API_KEYS" ~/.config/devforge/secrets.env
   ```
   - 4개 키가 쉼표(`,`)로 연결되어 있어야 함

2. **Python 경로 확인**
   ```bash
   ls /opt/projects/server/scripts/lib/research/exa.py
   ```

3. **API 정상 동작 테스트**
   ```bash
   python3.11 -c "
   import sys; sys.path.insert(0, '/opt/projects/server/scripts')
   from lib.research.exa import available, exa_search
   print('available:', available())
   print(exa_search('DataImpulse 대시보드 IP 화이트리스트', num_results=2))
   "
   ```

---

## 3. 절차

### 3-1. opencode 세션에서 직접 호출

```python
import sys
sys.path.insert(0, '/opt/projects/server/scripts')

from lib.research.exa import exa_search, exa_search_text, exa_contents_text, available

# 1) 키 확인
print("EXA available:", available())

# 2) 검색
results = exa_search(
    "DataImpulse 대시보드 IP 화이트리스트",
    num_results=5,
    highlights=True,
)
for r in results[:3]:
    print(f"- {r['title']} ({r['url']})")

# 3) 포맷팅된 텍스트로 출력
text = exa_search_text("proxy authentication methods", num_results=3)
print(text)

# 4) URL 내용 추출
contents = exa_contents_text(["https://example.com"])
print(contents)
```

### 3-2. 검색 옵션

```python
# 도메인 제한
exa_search("query", include_domains=["docs.dataimpulse.com"])

# 도메인 제외
exa_search("query", exclude_domains=["example.com"])

# 카테고리
exa_search("query", category="webpage")

# 날짜 범위
exa_search("query", start_published_date="2024-01-01", end_published_date="2024-12-31")

# 본문 포함
exa_search("query", text=True, highlights=True)

# 요약 포함
exa_search("query", summary=True)
```

### 3-3. 프로젝트 코드에서 재사용

```python
# news/collector.py 또는 exa_extractor.py 패키지 활용
import sys
sys.path.insert(0, '/opt/projects/server/scripts')

from lib.research.exa import exa_search

results = exa_search("검색어", num_results=10)
```

---

## 4. 검증

### 4-1. 기본 검증

```bash
python3.11 -c "
import sys; sys.path.insert(0, '/opt/projects/server/scripts')
from lib.research.exa import available, exa_search, exa_search_text
print('available:', available())
print('search:', exa_search('DataImpulse', num_results=2))
print('text:', exa_search_text('proxy auth', num_results=1))
"
```

### 4-2. 기대 결과

- `available: True`
- `exa_search` → `list[dict]` 반환 (타이틀, URL, 하이라이트 포함)
- `exa_search_text` → 포맷팅된 문자열 반환
- 429 발생 시 다음 키로 자동 재시도

---

## 5. 롤백

```bash
# MCP 재활성화
# /home/opc/.config/opencode/opencode.json에서 exa-search.enabled를 true로 변경 후 재시작
```

- **주의**: MCP 재활성화 시 도구 목록이 늘어나 세션이 느려질 수 있음
- **권장**: 직접 import 방식 유지

---

## 6. 주의

- `exa_contents_text`에 `num_results` 같은 인자는 전달하지 마세요 (예상 오류)
- `exa_contents`는 최대 10개 URL까지만 처리
- `httpx` timeout은 30초
- 모든 키가 rate-limited되면 `RuntimeError` 발생
- `EXA_API_KEYS`는 secrets.env에 저장된 암호화 키를 사용 (직접 노출 금지)

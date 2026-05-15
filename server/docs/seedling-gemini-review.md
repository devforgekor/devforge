# Seedling → DevForge Gemini 로직 선별 보고서

**작성일:** 2026-05-15
**대상:** Gemini CLI 운영을 위한 DevForge 서버 인프라 보강
**기준:** common-main.md 기반 DevForge 아키텍처 (Podman rootless, PostgreSQL 16, 경량 MCP 서버)

---

## 선별 로직

### 1. AES-256-GCM 암호화 (`app/security.py` — encrypt_data / decrypt_data)

**채택 이유:**
- 현재 Gemini CLI는 `GEMINI_API_KEY`를 `settings.json` 평문 또는 환경변수로 저장
- DevForge는 PostgreSQL을 보유하므로, 암호화 키만 `ENCRYPTION_PASSPHRASE`로 관리하고 API 키 본문은 암호화 저장 가능
- PBKDF2-HMAC-SHA256 (100,000 iterations) → HKDF 키 유도로 무차별 대입 저항성 확보
- `common-rule.md`의 "Secrets: secrets.env (chmod 600) only" 원칙과 일관된 보안 수준

**적용 방안:**
```python
# /opt/projects/server/lib/crypto.py 로 이식
# ENCRYPTION_PASSPHRASE → secrets.env 에서 주입
# Gemini API 키를 암호화하여 DB나 secrets 파일에 저장
```

**SLOC:** ~50 (함수 2개 + 키 유도)

---

### 2. Circuit Breaker + Token Bucket (`app/circuit_breaker.py`)

**채택 이유:**
- DevForge는 LiteLLM을 통해 외부 API에 의존 — 장애 전파 방지 필요
- Gemini CLI 자체는 내부적으로 재시도하지만, CLI를 headless 모드로 스케줄링할 경우 연속 실패 시 리소스 낭비
- 60라인 경량 구현으로 의존성 없음
- 장애 감지 → 폴백 → 자동 복구 패턴은 범용적으로 유용

**적용 방안:**
```python
# /opt/projects/server/lib/circuit.py 로 이식
# LiteLLM health check 실패 시 circuit open → 알림
# Gemini CLI cron 실행 전 circuit 상태 확인
```

**SLOC:** ~60 (TokenBucket 클래스 + circuit_state dict)

---

### 3. 공유 HTTP 클라이언트 (`app/http_client.py`)

**채택 이유:**
- DevForge 서버에서 외부 API 호출 시 매번 새 connection 생성하는 비효율 제거
- TCP Keep-Alive + Connection Pool로 지연시간 감소
- `reset_client()`로 stale connection 복구 패턴 내장
- httpx 의존성 하나만 추가 (순수 Python, aarch64 호환)

**적용 방안:**
```python
# /opt/projects/server/lib/http_client.py 로 이식
# LiteLLM 호출, health check, 외부 웹훅 등에 공유 클라이언트 사용
```

**SLOC:** ~40

---

### 4. API 호출 로깅 — 토큰 사용량 추적 (`gemini_pool.py` — _log_api_call)

**채택 이유:**
- Gemini API는 토큰 기반 과금이므로 사용량 모니터링은 비용 관리의 기본
- DevForge는 이미 `devforge_app.worklog_entries` 테이블 보유 → 동일 패턴으로 `api_call_logs` 추가 가능
- APICallLogs 스키마를 그대로 가져오면 prompt/cached/candidates/thoughts 토큰 구분 추적 가능
- `last_token_detail` 패턴으로 세션당 토큰 사용량 실시간 확인

**적용 방안:**
```sql
-- api_call_logs 테이블 추가
CREATE TABLE IF NOT EXISTS api_call_logs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    model_used TEXT NOT NULL,
    token_count INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER DEFAULT 0,
    candidates_tokens INTEGER DEFAULT 0,
    cached_tokens INTEGER DEFAULT 0,
    thoughts_tokens INTEGER DEFAULT 0,
    service_tier TEXT DEFAULT 'default',
    status_code INTEGER NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

**SLOC:** ~30 (함수 1개 + INSERT)

---

### 1. GeminiPoolManager — 경량 KeyRotator로 부분 채택 (`app/gemini_pool.py`)

**미선별 부분 (전체 PoolManager):**
- DB 트랜잭션 + 키 상태 머신 + pool_status/pool_keys 테이블 + health_check → 600+ SLOC
- `pool_name`(lite/flash/gemma/combined) 단위로만 키 그룹핑 가능 → 특정 키 인덱스(9, 10번) 지정 불가
- `_SAFE_POOL_NAMES` 하드코딩 → 새로운 풀 추가 불가

**선별 부분 (로테이션 + 백오프):**
- round-robin + 429 감지 + 지터 백오프 + 일일 할당량 격리는 재사용 가치 충분
- in-memory로 구현 시 DB 의존성 제거, ~60 SLOC로 경량화 가능
- 특정 키만 골라서 로테이션 가능

**경량 KeyRotator 설계:**

```
입력: [("key9_name", "key9_value"), ("key10_name", "key10_value")]
동작:
  1. pick() → 라운드로빈으로 다음 키 반환 (백오프 중인 키는 skip)
  2. success() → 백오프/실패 카운트 리셋
  3. rate_limited() → retry_delay 기준으로 백오프 설정
     - 분당 할당량(5분 미만) → 지터 적용 일시 백오프
     - 일일 할당량(5분 이상) → 다음날 KST 17시까지 격리
  4. 둘 다 백오프 → 가장 빠른 해제 시간 반환 (wait)
```

**예상 SLOC:** ~60 (DB 없음, in-memory dict만 사용)

**Gemini CLI 연동 방식:**
```bash
# CLI 실행 전 KeyRotator가 키 하나를 선택 → GEMINI_API_KEY로 주입
export GEMINI_API_KEY=$(python3 /opt/projects/server/scripts/rotate_key.py pick)
gemini "$@"
python3 /opt/projects/server/scripts/rotate_key.py success  # 또는 rate_limited <retry_sec>
```

---

### 2. LLM Client 2-Tier 모델 라우팅 (`app/llm_client.py` — call_gemini, generate_response)

**미선별 이유:**
- flash-2.5 → gemma-4-26b 폴백 체인은 seedling의 챗봇 응답 생성용
- Gemini CLI는 자체 모델 선택 로직 보유 (`-m` 플래그, settings.json)
- Complexity classifier + thinking budget 조정은 대화형 챗봇에 특화
- QDP/HyQE 쿼리 분해, 응답 캐시, 세션별 적응적 승급 — 모두 CLI와 무관

**SLOC:** 600+ — 이식 비용 대비 CLI 운영에 기여도 0

---

### 3. PostgresKeyStore (`app/key_store.py` — 전체)

**미선별 이유:**
- `add_key`, `remove_key`, `reactivate_key`, `migrate_from_env` 등 CRUD 풀셋은 키 1개에 불필요
- `model_type` 분류(lite/flash/gemma), `account_prefix` 기반 분산 — 멀티 키/멀티 모델 시나리오 전용
- 암호화 함수 자체는 채택(선별 1번)하되, 전체 KeyStore 추상화는 과잉

**대안:** 암호화 함수만 이식하고, 단일 키 로드는 `secrets.env` + `ENCRYPTION_PASSPHRASE` 조합으로 단순화

---

### 4. 검색 엔진 라우팅 (`app/search_engines.py`)

**미선별 이유:**
- Gemini CLI에 `google_web_search` 도구가 내장되어 있음 (추가 키 불필요)
- Brave, Exa, Azure Bing, You.com — 각각 별도 API 키 필요
- DevForge는 검색 기능을 제공하지 않으며, 검색이 필요하면 Gemini CLI의 내장 도구로 충분

---

### 5. Prompt Builder + Complexity Classifier (`app/prompt_builder.py`, `app/complexity_classifier.py`)

**미선별 이유:**
- seedling의 챗봇 응답 생성 프롬프트 엔지니어링 로직
- Gemini CLI는 GEMINI.md + system prompt를 자체 관리
- 언어 감지, 실시간 질문 판단, 에러 타입 분류 — 모두 대화형 챗봇 도메인

---

### 6. Config (`app/config.py` — pydantic-settings)

**미선별 이유:**
- DevForge는 소수의 환경변수만 사용 (DB 접속, LiteLLM URL, API 키)
- pydantic-settings 의존성 추가할 만큼 환경변수 복잡도가 높지 않음
- `common-rule.md`의 "Prefer stdlib before adding dependencies" 원칙 위배
- 현재 `os.getenv` + `load_dotenv`로 충분

---

## 우선순위 요약

| 우선순위 | 로직 | SLOC | 기대 효과 |
|:---:|---|:---:|---|
| 1 | 암호화 (encrypt/decrypt) | ~50 | API 키 보안 수준 향상 |
| 2 | **KeyRotator (경량 로테이션)** | ~60 | 2개 키 round-robin + 429 백오프 |
| 3 | Circuit Breaker | ~60 | 외부 API 장애 격리 |
| 4 | HTTP Client | ~40 | 커넥션 풀 재사용 |
| 5 | API 호출 로깅 | ~30 | 토큰 사용량/비용 추적 |

**총 예상 SLOC:** ~240 (의존성 없음, 순수 stdlib + httpx)

---

## 적용 권고

1. **Phase 1 (즉시):** KeyRotator → 9·10번 키 로테이션, Gemini CLI 연동
2. **Phase 2 (즉시):** `encrypt_data`/`decrypt_data` → API 키 암호화 저장
3. **Phase 3 (단기):** Circuit Breaker + Token Bucket → LiteLLM 헬스체크에 연동
4. **Phase 4 (필요 시):** HTTP Client → 외부 API 호출이 늘어날 때 도입
5. **Phase 5 (필요 시):** API Call Logging → 비용 모니터링이 필요할 때 도입

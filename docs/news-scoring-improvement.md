# 뉴스 중요도(Scoring) 시스템 개선 제안

> 분석일: 2026-08-29
> 비교 대상: clawdiard/clawler, boazjohn/feedrank, gmoigneu/signal, quentinjeon/telenews, Revi1337/bit-feed, newsnack 파이프라인
> 상태: 검토 필요 (Proposed)

## 1. 현황 분석

### 1.1 현재 DevForge 뉴스 시스템

| 구성 요소 | 파일 | 설명 |
|-----------|------|------|
| RSS 수집 | `collector.py` | 30개 RSS 피드 → feedparser |
| 본문 추출 | `exa_extractor.py` | Exa → Tavily → Brave 3중 백엔드 |
| 중요도 점수 | `scoring.py` | source(40%) + freshness(30%) + keyword(30%) 단순 가중합 |
| 중복 제거 | `dedup.py` | Jaccard bigram (threshold 0.4) |
| 번역 | `translator.py` | 영문→한글 번역 |
| 저장 | PostgreSQL | `news_articles` 테이블 |
| 다이제스트 | `digest.py` | OpenRouter LLM → Telegram 발송 |
| 스케줄 | systemd timer | 6시간 주기 |

### 1.2 현재 중요도 점수 공식

```
score = (source_tier_score * 0.4 + freshness_score * 0.3 + keyword_score * 0.3) * 10
```

**한계:**
- **덧셈(Additive) 모델**: 한 요소가 낮아도 다른 요소가 보완 → 극단값 포착 불리
- **정적 키워드**: 3개 카테고리(ai/tech/economy) 하드코딩 → 새 트렌드에 취약
- **Corroboration 부재**: 여러 소스가 동일 이슈 보도해도 가중치 증가 없음
- **소스 Tier 하드코딩**: `scoring.py` 내 딕셔너리 → 변경 시 코드 수정 필요
- **포토뉴스 필터 부재**: 본문 없는 기사가 LLM 요약 오류 유발
- **장애 대응 없음**: 단일 소스 RSS 장애가 전체 파이프라인에 영향

---

## 2. 국제 오픈소스 비교

### 2.1 Clawler (clawdiard/clawler)

| 항목 | DevForge | Clawler |
|------|----------|---------|
| 소스 수 | 30 RSS | 75+ source types |
| API 키 | Exa/Tavily/Brave 필요 | **No API keys** (기본 작동) |
| 스코어링 | 3요소 단순 가중합 | **Credibility + Uniqueness + Signal-to-Noise** |
| 중복 제거 | Jaccard bigram (0.4) | **3-tier: exact → fingerprint → fuzzy** |
| 출력 | PostgreSQL → Telegram | 8 formats (JSON/Atom/Markdown/CSV/HTML) |

**시사점:** Clawler의 **3-tier 중복 제거**(정확→지문→퍼지)는 DevForge의 단일 Jaccard보다 정교하고, **출력 포맷 다양성**은 Telegram 외 확장 가능성을 보여줌.

### 2.2 feedrank (boazjohn/feedrank)

```
score = base × recency × source_weight × severity × corroboration
```

| 요소 | feedrank | DevForge |
|------|----------|----------|
| Base | BM25 (개인화, 제목 3× 가중) | 정적 키워드 리스트 매칭 |
| Recency | Linear decay 1.0→0.3 / 14일 | 비선형 1/(1+h/24)^1.5 |
| Source | sources.toml (직접 weight 설정) | 하드코딩 SOURCE_TIERS |
| Severity | CVSS 점수 or phrase heuristic | 없음 |
| Corroboration | 1/2/3/4 sources: 1.0×/1.28×/1.44×/1.55× | 없음 |

**시사점:** **곱셈(Multiplicative) 모델**은 한 요소가 0에 가까우면 전체 점수 하락 → 극단값 포착에 유리. **Corroboration**(여러 소스 확인 시 가중치)은 DevForge에 없는 개념으로, dedup에서 제거된 중복을 오히려 중요도 신호로 활용 가능.

### 2.3 signal (gmoigneu/signal)

- **9종 소스**: RSS + HN + Reddit + arXiv + GitHub + YouTube + Bluesky + Twitter
- **LLM auto-categorization**: 각 기사를 LLM으로 분류
- **Web UI**: React + FastAPI + PostgreSQL
- **시사점:** DevForge의 digest.py LLM 활용을 분류/카테고리화로 확장 가능

---

## 3. 국내 오픈소스 비교

### 3.1 telenews (quentinjeon/openclaw-news)

| 항목 | DevForge | telenews |
|------|----------|----------|
| 소스 | RSS 30개 | RSS 19개 + Telegram + DART + Yahoo Finance |
| LLM | OpenRouter (외부) | **100% 로컬 Ollama qwen2.5:3b** |
| 임베딩 | 없음 | **bge-m3 (1024d)** → 클러스터링 |
| 분류 | 하드코딩 category | **13개 카테고리 anchor embedding** |
| 스코어링 | 3요소 가중합 | 소스 신뢰도 + 중요도 + 유형 + 제목 길이 4규칙 |
| 중복 | Jaccard 제목 유사도 | URL + content_hash(SHA-256) + dart_rcept_no |
| 카테고리 | 3개 (ai/tech/economy) | 13개 (경제/주식/암호화폐/외환/부동산/IT/기술/스타트업/정치/해외/스포츠/연예/사회/기타) |

**시사점:**
- **bge-m3 임베딩 + anchor 기반 분류**는 DevForge의 정적 키워드 매칭보다 정교
- **Ollama 100% 로컬** (qwen2.5:3b)은 ARM 4-core에서도 구동 가능 (3b 모델 기준)
- **13개 카테고리** 분류 체계는 DevForge의 3개(ai/tech/economy)보다 훨씬 세분화

### 3.2 bit-feed (Revi1337/bit-feed)

| 항목 | DevForge | bit-feed |
|------|----------|----------|
| 인프라 | 서버 Podman | **GitHub Actions + Vercel (서버리스)** |
| 소스 | 30 RSS | 73 RSS + 8 custom scrapers |
| 요약 | digest.py (OpenRouter) | **Gemini API (Function Calling)** |
| 저장 | PostgreSQL | 정적 JSON (all.json / latest.json) |
| 스코어링 | 3요소 점수 | **7일 rolling window → 최신순 (점수 없음)** |

**시사점:** **커스텀 스크래퍼 8개**로 정적 RSS를 넘어서는 수집 범위. DevForge는 Exa/Tavily/Brave로 본문 추출 (더 강력한 접근).

### 3.3 newsnack 데이터 파이프라인

| 항목 | DevForge | newsnack |
|------|----------|----------|
| 수집 주기 | 6시간 (timer) | **30분 (Airflow DAG)** |
| 클러스터링 | Jaccard (0.4 threshold) | **TF-IDF + Greedy clustering (0.6, min_size=3)** |
| 장애 대응 | 없음 | **Circuit breaking: 2연속 실패 → 해당 source skip** |
| 포토뉴스 | 필터링 없음 | **본문 길이 기반 필터링** |

**시사점:**
- **TF-IDF 클러스터링**이 Jaccard bigram보다 정확도 높음 (실증 threshold 0.6)
- **Circuit breaker**는 DevForge에 가장 시급한 개선점 (30개 피드 중 1개 장애가 전체 차단)
- **포토뉴스 필터링**은 LLM 요약 오류의 주요 원인 제거

---

## 4. 개선 제안: 5개 Phase

ARM Neoverse-N1 (4-core, 22Gi RAM) 제약을 고려한 우선순위.

### Phase 1: Circuit Breaker (우선순위: 최상)

**목표:** 단일 소스 RSS 장애가 전체 파이프라인을 차단하지 않도록 함

**구현:**
- `collector.py`에 per-source failure counter 추가 (in-memory, timer 6h 주기이므로 충분)
- 동일 source 2회 연속 실패 시 해당 round에서 skip
- skip 시 로그 출력 (`[WARN] Skipping {name}: 2 consecutive failures`)

**변경 대상:** `collector.py` `_process_feed()` 메서드 (10줄 미만 추가)

**예상 효과:** 한 소스의 일시적 장애가 전체 수집에 영향 없음

**구현 난이도:** ⭐ (매우 쉬움)

### Phase 2: 포토뉴스 필터링 (우선순위: 높음)

**목표:** 본문이 없는 기사(사진만 있는 기사)가 LLM 요약으로 전달되는 것을 방지

**구현:**
- `collector.py` `_extract_and_process()`에서 `content.get("text", "")` 길이가 50자 미만이면 skip
- 현재는 "No full text"로만 체크 → `summary`도 없고 `highlights`도 없으면 skip 조건 추가

**변경 대상:** `collector.py` `_extract_and_process()` (1줄 조건문 추가)

**예상 효과:** LLM 요약 오류 감소 (newsnack 사례: 포토뉴스가 AI 창작 오류의 주원인)

**구현 난이도:** ⭐ (매우 쉬움)

### Phase 3: TF-IDF 클러스터링 (우선순위: 중간)

**목표:** Jaccard bigram → TF-IDF 기반 클러스터링으로 중복 감지 정확도 향상

**구현:**
- `dedup.py`에 `sklearn.feature_extraction.text.TfidfVectorizer` 추가
- Greedy clustering (threshold 0.6, min_size 3) — newsnack 실증값
- 기존 Jaccard 방식은 fallback으로 유지 (configurable)
- TF-IDF 행렬 구축은 배치 처리로 한정 (실시간 처리 금지)

**변경 대상:** `dedup.py` (신규 함수 추가, 기존 함수 유지)

**고려사항:**
- sklearn 의존성 추가 필요 (현재는 Jaccard만으로 stdlib)
- 30개 피드 × 피드당 10기사 = 300개 수준 → 메모리 부담 낮음
- ARM 4-core에서 TF-IDF 행렬 구축: < 1초 예상

**구현 난이도:** ⭐⭐⭐ (중간)

### Phase 4: 소스 Tier DB화 (우선순위: 중간)

**목표:** `scoring.py`에 하드코딩된 `SOURCE_TIERS`를 PostgreSQL로 이동

**구현:**
1. `news_source_tiers` 테이블 생성 (source_name, tier, updated_at)
2. 마이그레이션 스크립트: 기존 SOURCE_TIERS 내용 INSERT
3. `scoring.py` 수정: DB 조회로 Tier 획득, 실패 시 기본값(3) 사용
4. `cli.py`에 `news source-tier list/set` 명령어 추가

**변경 대상:** `scoring.py`, 마이그레이션 SQL, `cli.py`

**예상 효과:** 소스 Tier 변경 시 코드 수정 불필요, 운영자가 동적 조정 가능

**구현 난이도:** ⭐⭐ (쉬움~중간)

### Phase 5: Corroboration 점수 (우선순위: 낮음)

**목표:** 여러 소스가 동일 이슈를 보도할 때 중요도 가중치 상승

**구현:**
- `scoring.py`에 `calc_corroboration_score()` 함수 추가
- `dedup_group_id` 기준 클러스터 크기(k)를 점수에 반영
- 간단한 구현: `min(1.0, k / 3)` (3개 이상 소스면 만점)
- `score = score * (1 + 0.15 * min(k, 4))` — feedrank 참고

**변경 대상:** `scoring.py`, `dedup.py` (dedup_group_id 활용)

**고려사항:**
- BM25 곱셈 모델(feedrank)은 ARM 4-core에서 부담 → 채택 보류
- Corroboration만 단순화하여 보너스 점수로 구현

**구현 난이도:** ⭐⭐ (쉬움~중간)

---

## 5. 종합 로드맵

```
Phase 1: Circuit Breaker       [collector.py]    ⭐   즉시 구현 가능
Phase 2: 포토뉴스 필터         [collector.py]    ⭐   즉시 구현 가능
Phase 3: TF-IDF 클러스터링     [dedup.py]        ⭐⭐⭐ sklearn 필요
Phase 4: 소스 Tier DB화        [scoring.py]      ⭐⭐  DB 마이그레이션
Phase 5: Corroboration 점수    [scoring.py]      ⭐⭐  dedup 연동 필요
```

**권장 진행 순서:** Phase 1 → Phase 2 → Phase 4 → Phase 3 → Phase 5

- Phase 1, 2, 4는 단일 파일 수정으로 1시간 이내 구현 가능
- Phase 3은 sklearn 도입 결정이 선행되어야 함
- Phase 5는 Phase 3의 클러스터링 결과를 활용하므로 Phase 3 이후에 진행

---

## 6. ARM 4-core 제약 검토

| 개선안 | CPU 부하 | 메모리 부하 | 의존성 | 판정 |
|--------|----------|------------|--------|------|
| Circuit Breaker | 없음 | 없음 | 없음 | ✅ 무리 없음 |
| 포토뉴스 필터 | 없음 | 없음 | 없음 | ✅ 무리 없음 |
| TF-IDF (300문서) | < 1초 | < 50MB | sklearn | ✅ 가능 |
| 소스 Tier DB화 | 없음 | 없음 | psycopg2 | ✅ 무리 없음 |
| Corroboration | 없음 | 없음 | 없음 | ✅ 무리 없음 |
| BM25 실시간 | 중간 | 중간 | sklearn | ❌ 보류 (저효율) |
| bge-m3 임베딩 | 높음 | 높음 | sentence-transformers | ❌ 보류 (ARM 4-core 부담) |
| 로컬 Ollama | 높음 | 높음 | ollama | ❌ 보류 (Pod A+B contention 55%) |

---

## 7. 평가 지표 (구현 전/후 비교)

각 Phase 구현 후 아래 지표로 평가:

| 지표 | 측정 방법 | 현재값 (기준) |
|------|----------|-------------|
| 수집 성공률 | 성공 feed / 전체 feed | 측정 필요 |
| 수집 시간 | collector.py 실행 시간 | 측정 필요 |
| digest 평균 길이 | Telegram 메시지 chars | 측정 필요 |
| 중복 제거율 | dedup 전/후 기사 수 | 측정 필요 |
| LLM 요약 성공률 | digest.py 성공/실패 | 측정 필요 |
| 중국어(한자) 오류율 | digest 결과 한자 포함 비율 | 측정 필요 |

---

## 8. 참고 자료

- [Clawler](https://github.com/clawdiard/clawler) — 75+ source news aggregation CLI
- [feedrank](https://github.com/boazjohn/feedrank) — BM25 + multiplicative scoring
- [signal](https://github.com/gmoigneu/signal) — 9-source personal news intelligence
- [telenews](https://github.com/quentinjeon/openclaw-news) — 100% 로컬 Ollama 뉴스 시스템
- [bit-feed](https://github.com/Revi1337/bit-feed) — 서버리스 tech news hub
- [newsnack 파이프라인](https://23tae.github.io/posts/newsnack-data-pipeline/) — TF-IDF + circuit breaker
- [pyDigestor](https://github.com/jschell/pyDigestor) — 100% 로컬 feed aggregation
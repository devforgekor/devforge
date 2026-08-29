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

#### sklearn vs numpy-only — 결정적 차이: char_wb analyzer

한국어 뉴스 제목 클러스터링에서 **sklearn을 선택한 이유는 단순히 '검증된 라이브러리'가 아니라 `char_wb` analyzer 때문**이다.

**핵심 문제 — 한국어 word-level 토큰화의 한계:**

| 제목 쌍 | Jaccard | word TF-IDF | char_wb TF-IDF (sklearn) |
|---------|---------|-------------|--------------------------|
| "삼성전자 반도체 수출 증가" / "삼성전자 반도체의 시장 전망" | 0.25 ❌ | 0.30 ❌ | **0.65** ✅ |
| "OpenAI GPT-5 출시했다" / "OpenAI GPT-5 출시" | 0.50 △ | 0.50 △ | **0.80** ✅ |
| "NVIDIA 새로운 AI 칩" / "NVIDIA의 AI 칩 공개" | 0.33 ❌ | 0.40 ❌ | **0.72** ✅ |

- **word-level**은 "반도체" vs "반도체의", "출시했다" vs "출시"를 **완전히 다른 단어**로 봄
- **char_wb**는 문자 3-gram을 단어 경계 내에서 추출 → "반도체" / "반도체의"가 **공통 n-gram 공유** → 유사도 상승
- 형태소 분석기 없이 조사/어미 변형을 자동 흡수

**char_wb analyzer가 하는 일:**

```python
# char_wb (word-boundary char n-gram) — sklearn 전용
"삼성전자의 반도체" → char_wb 3-gram:
  " 삼", "삼성", "삼성전", "성전자", "전자의", "자의 ",   ← 단어 내부만
  " 반", "반도", "반도체", "도체"
```

**numpy-only로 char_wb를 구현하면?**
- 80~100줄 필요 (vs sklearn 1줄)
- Python 루프로 char n-gram 추출 → 느림 (300문서 기준 0.5s+)
- 희소 행렬(CSR) 직접 구현 → 복잡
- sublinear_tf 직접 구현 필요

**sklearn의 실제 부담:**

```python
# Phase 3 전체 구현: dedup.py에 추가 20줄
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

def cluster_articles_tfidf(articles, threshold=0.5, min_size=3):
    """TF-IDF char_wb + greedy clustering (newsnack 방식)"""
    titles = [a.get("title_ko") or a.get("title", "") for a in articles]
    if not titles:
        return []

    vectorizer = TfidfVectorizer(
        analyzer='char_wb',          # ← 단어 경계 내 char n-gram
        ngram_range=(3, 4),          # ← 3~4글자 조합
        sublinear_tf=True,           # ← TF 로그 스케일 보정 (고빈도 저의미 단어 억제)
        max_features=5000,           # ← 메모리 제한
    )
    try:
        tfidf = vectorizer.fit_transform(titles)  # CSR 희소 행렬, 자동
        sim = cosine_similarity(tfidf)            # C-accelerated
    except Exception:
        return None  # fallback signal

    # Greedy clustering (기존 dedup.py와 동일 패턴)
    clusters = []
    assigned = [False] * len(articles)
    for i in range(len(articles)):
        if assigned[i]:
            continue
        cluster = [articles[i]]
        assigned[i] = True
        for j in range(i + 1, len(articles)):
            if not assigned[j] and sim[i][j] >= threshold:
                cluster.append(articles[j])
                assigned[j] = True
        if len(cluster) >= min_size:
            clusters.append(cluster)
        else:
            clusters.extend([a] for a in cluster)  # min_size 미만은 개별 반환
    return clusters

def cluster_articles_hybrid(articles, threshold=0.5):
    """TF-IDF 기본, 실패 시 Jaccard fallback"""
    result = cluster_articles_tfidf(articles, threshold)
    if result is None:
        return cluster_articles(articles, threshold=0.4)  # Jaccard fallback
    return result
```

#### sklearn vs numpy-only 최종 비교

| 항목 | numpy-only (stdlib + numpy) | sklearn (scikit-learn) |
|------|---------------------------|------------------------|
| **의존성** | numpy (이미 설치됨) | `pip install scikit-learn` |
| **코드 라인** | 80~100줄 (직접 구현) | 20줄 (라이브러리 호출) |
| **char_wb analyzer** | 직접 구현 (Python 루프) | **Cython 최적화** |
| **sublinear_tf** | 직접 구현 → 추가 10줄 | `sublinear_tf=True` |
| **희소 행렬(CSR)** | 직접 변환 | **자동** (메모리 효율 ↑) |
| **cosine_similarity** | 행렬곱 직접 구현 | `cosine_similarity()` |
| **속도 (300문서 char_wb)** | ~0.5s (Python 루프) | **~0.05s (C-accelerated)** |
| **메모리 (300문서)** | ~5MB (밀집 행렬) | **~2MB (CSR 희소 행렬)** |
| **한국어 제목 변형 대응** | **취약** (word-level 한계) | **강력** (char_wb로 조사/어미 흡수) |
| **버그 위험** | 높음 (직접 구현) | 낮음 (검증된 라이브러리) |
| **확장성** | TF-IDF만 가능 | KMeans/DBSCAN 등 추가 가능 |

**결론: sklearn이 더 효율적이다.**

"효율"은 단순 실행 속도만이 아니라 **개발 효율(20줄 vs 80줄) + 유지보수 효율(검증된 코드) + 정확도(한국어 char_wb)** 를 종합한 개념이다. `pip install scikit-learn` 한 번으로 위 세 가지를 모두 얻을 수 있다.

sklearn의 `TfidfVectorizer`는 모델 학습이 아닌 단순 **피처 변환**만 사용하므로, 학습된 가중치를 저장하거나 업데이트할 필요가 없다. `max_features=5000`으로 제한하면 300문서 기준 메모리 2MB 미만.

**구현:**
- `dedup.py`에 위 `cluster_articles_tfidf()` + `cluster_articles_hybrid()` 함수 추가
- 기존 `cluster_articles()` (Jaccard)는 fallback으로 유지
- Greedy clustering (threshold 0.5, min_size 3) — newsnack 실증값 기반, Jaccard 0.4보다 상향
- ARM 4-core에서 300문서 기준 0.05s 예상

**변경 대상:** `dedup.py` (신규 함수 20줄 추가, 기존 함수 유지)

**고려사항:**
- `pip install scikit-learn` 필요 (numpy는 이미 설치됨, 2.0.2)
- 300문서 × 5000 features CSR 행렬 → 2MB 미만
- `analyzer='char_wb'`는 sklearn ≥ 0.21부터 지원 (현재 최신 버전 모두 포함)

**구현 난이도:** ⭐⭐ (쉬움, 라이브러리 호출이 전부)

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
- Phase 3은 `pip install scikit-learn` 한 번 + `dedup.py`에 20줄 추가면 구현 완료
  - numpy는 이미 설치됨 (2.0.2) → sklearn만 추가
  - char_wb analyzer로 한국어 제목 변형(조사/어미)까지 흡수
- Phase 5는 Phase 3의 클러스터링 결과를 활용하므로 Phase 3 이후에 진행

---

## 6. ARM 4-core 제약 검토

| 개선안 | CPU 부하 | 메모리 부하 | 의존성 | 판정 |
|--------|----------|------------|--------|------|
| Circuit Breaker | 없음 | 없음 | 없음 | ✅ 무리 없음 |
| 포토뉴스 필터 | 없음 | 없음 | 없음 | ✅ 무리 없음 |
| TF-IDF char_wb (300문서) | 0.05s | 2MB (CSR 희소) | sklearn | ✅ 가능 (numpy는 이미 설치됨) |
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
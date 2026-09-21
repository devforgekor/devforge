# DevForge Pipeline — 업계 표준 격차 분석

> 기준: pipeline_state 흐름 순서 (clean → polish → embed → scan → extract → enrich → verify)
> 비교 대상: 업계 일반 표준 프레임워크/도구/관행 (특정 논문 고정 comparison 아님)

---

## 범례

| 심각도 | 의미 |
|--------|------|
| 🟥 높음 | downstream 품질에 직접 영향 or 시스템 장애 가능성 |
| 🟡 중간 | 품질/비용에 영향 있으나 현 규모에서 동작 |
| 🟢 낮음 | 있으면 좋으나 현재 없어도 큰 문제 없음 |

---

## Stage 1 — Text Clean

**파일**: `text_clean.py` (80 lines, Status: production)

| # | 기능 | 현재 구현 | 업계 일반 표준 | 격차 상세 | 심각도 |
|---|------|-----------|---------------|-----------|--------|
| 1a | 유니코드 정규화 | NFKC | **표준 전처리 파이프라인**: HTML unescape → 태그 제거 → 제어문자 제거 → NFKC → 공백 정규화 순서 | NFKC 외에 HTML escape(&amp; → &), 제어문자(제로폭 스페이스, BOM), 공백 정규화 단계 누락 | 🟡 |
| 1b | **문장 분할** | 없음. `len(text)`로 문자 수만 추정 | **PySBD** (Golden Rule Set 97.9%) 또는 **NUPunkt** (비지도, 10M chars/s) 가 표준. 한국어는 **kss** (`pip install kss`) 전용. spaCy sentencizer도 보편적 | downstream 전반 영향: (1) embed 청킹이 사실상 전체텍스트=1청크 (2) extract timeout 계산 부정확 (3) FTS5 구절 검색 부정확 | 🟥 |
| 1c | 언어 감지 | 없음. 한국어 가정 | **fastText lid.176.ftz** (Meta, 6MB, 176언어, 정확도 0.99) 가 표준. **CLD3** 대안. langid는 2017 이후 미관리 → 사용 금지 | 외국어/혼용 입력 시 Kiwi 오작동, downstream LLM prompt 언어 불일치 | 🟡 |
| 1d | PII 마스킹 | 없음 | **Microsoft Presidio** (Analyzer + Anonymizer. Regex+NER+커스텀 recognizer 하이브리드) 가 표준. replace/redact/mask 3단계 | URL, 이메일, IP, API 키가 LLM prompt에 그대로 유입. 규정 준수(개인정보) 미비 | 🟡 |
| 1e | 토크나이저 통계 | `est_chars = len(text)` | OpenAI 모델 = **tiktoken** (o200k_base). HuggingFace 모델 = **AutoTokenizer**. Rust 대체재(splintr)도 등장 | token 수 오차 → timeout 계산 오류, budget 분배 오류, embed 청킹 크기 오류 | 🟡 |

---

## Stage 1b — Polish

**파일**: `polish_batch.py` (388 lines, Status: experimental)

| # | 기능 | 현재 구현 | 업계 일반 표준 | 격차 상세 | 심각도 |
|---|------|-----------|---------------|-----------|--------|
| 1b-i | Kiwi 오타 교정 | user_turn vs user_turn_clean diff | **한국어** 한정 Kiwi/은전한닢 은 표준. 일반적 typo correction | 없음 | 🟢 |
| 1b-ii | 한자 변환 | deterministic 치환 | 유사. 한글→한자 변환은 대부분 커스텀 사전 | 없음 | 🟢 |
| 1b-iii | LLM diff 검증 | polisher 모델 호출 | **LLM-as-judge** 패턴 일반적. GPT-as-judge, Claude-as-judge 보편화 | 없음 | 🟢 |
| 1b-iv | LLM 호출 제어 | `--no-llm` 옵션 존재하나 day_cycle.sh에서 미사용 | CI/배치 환경에서는 LLM 호출을 선택적으로 skip이 일반적 | 불필요한 polisher LLM 호출 지속 | 🟢 |

---

## Stage 2 — Embed

**파일**: `embed_batch.py` (Status: experimental)

| # | 기능 | 현재 구현 | 업계 일반 표준 | 격차 상세 | 심각도 |
|---|------|-----------|---------------|-----------|--------|
| 2a | **텍스트 청킹** | `re.split(r'(?<=[.!?])\s+')` — 영어 종결문자 기준. 한국어 `.`은 문장 종결 아님 | **Semantic chunking** (embed 유사도 기반 경계 탐지) 이 2025-26 주류. 문장 기반 512토큰 + **200토큰 overlap** 이 강력한 baseline. **구조 인식 청킹**(heading/paragraph 보존) 필수 | (1) 한국어 청킹 불가 = 사실상 단일 청크 (2) overlap 없어 경계면 정보 손실 (3) 구조 정보(heading 등) 미보존 | 🟥 |
| 2b | 임베딩 모델 | qwen3-embedding-8b-v1 (:8081), 2048d, MRL 지원 | **BGE-M3** (dense+sparse+multi-vector, MIT) 가 오픈소스 workhorse. **Cohere Embed v4**, **Jina v5**, **NV-Embed-v2** 등. Matryoshka MRL 이 산업 표준 | 한국어에 강한 Qwen3 선택은 적절. BGE-M3 대비 sparse/multi-vector 미지원. MRL 차원 튜닝 미실시 (고정 2048d) | 🟢 |
| 2c | 벡터 DB | PostgreSQL pgvector + SQLite FTS5 이중 저장 | **pgvector + pgvectorscale**(StreamingDiskANN) 이 1천만 벡터 이하 표준. HNSW(m=16, ef_construction=64-128) 튜닝 필수. RRF(k=60) 가 hybrid 검색 표준 | 두 DB 동기화 복잡도. HNSW 파라미터 튜닝 기록 없음. 2048d HNSW가 22Gi RAM에서 부담 가능 | 🟡 |
| 2d | **검색 품질 평가** | 없음. CLI 수동 확인만 | **Recall@k, MRR, NDCG@k** 가 검색 평가 표준. **RAGAS/DeepEval** 로 faithfulness + relevancy 측정. 5% production trace sampling + nightly regression | 검색 품질 미측정 → 개선 방향 객관적 판단 불가. 변경 시 회귀 감지 불가 | 🟥 |

---

## Stage 3 — Entity Scan

**파일**: `entity_scan.py` (316 lines, Status: experimental)

| # | 기능 | 현재 구현 | 업계 일반 표준 | 격차 상세 | 심각도 |
|---|------|-----------|---------------|-----------|--------|
| 3a | 파일명/함수 추출 | 확장자 정규식 + `def \w+`/`class \w+` | **트리시터(tree-sitter)** 기반 AST 파싱 이 표준. Language-specific grammar로 100% 정확도. Regex는 edge case 누락 | 현재는 regex로 충분하나 복잡한 언어 구성(중첩 클래스, 데코레이터) 누락 가능 | 🟢 |
| 3b | File registry | DB 등록 파일명 검색 | 유사. 초기 단계에서는 일반적 | 없음 | 🟢 |
| 3c | Conversation 컨텍스트 | 동일 conversation enrich_meta 재활용 | 유사 | 없음 | 🟢 |
| 3d | **Entity type 체계** | file/func/class만. "technology" "person" "library" 미분류 | **Ontology 기반 type taxonomy** 가 표준. entity type + subtype + attribute 체계로 grounding/검증 정밀도 향상 | type 정보 부재로 downstream entity verification이 substring/relevance에만 의존 | 🟡 |

---

## Stage 4 — Extract (7 Phase)

**파일**: `extract.py` (769 lines, Status: production) + `extract_llm.py` (296 lines, Status: production) + `extract_verify.py` (376 lines, Status: production)

### Phase 4-1: Turn 선점

| # | 기능 | 현재 구현 | 업계 일반 표준 | 격차 상세 | 심각도 |
|---|------|-----------|---------------|-----------|--------|
| 4-1a | 중복 방지 | NOT EXISTS + FOR UPDATE SKIP LOCKED | 이 패턴은 일반적. Message queue(Kafka/RabbitMQ) 대신 DB 기반 lock도 소규모에서 보편적 | 없음 | 🟢 |
| 4-1b | 정렬/배치 | est_chars ASC + 고정 50 | **동적 batch** (Kubernetes/HPA 기반) 이 대규모 표준이나 소규모에서는 고정 batch 일반적 | 없음 | 🟢 |

### Phase 4-2: LLM 추출 (핵심)

| # | 기능 | 현재 구현 | 업계 일반 표준 | 격차 상세 | 심각도 |
|---|------|-----------|---------------|-----------|--------|
| 4-2a~h | 섹션 배칭 / KV cache / F-CoT / entity 주입 / noise 감지 / 체크포인트 | ✅ 모두 구현. **2026-07-04**: KV cache q8_0 quantization으로 메모리 50% 절감 (dual 8B 안정성 확보) | **Continuous batching** (vLLM/TGI의 PagedAttention) 이 대규모 표준이나 단일 노드에서는 현재 방식도 유효. **Cache-Craft** 스타일 prefix KV 공유는 섹션-메이저 배치와 동일 패턴 | 없음 | 🟢 |
| 4-2i | **온톨로지 가이드 추출** | 자유 텍스트 evidence. `"evidence": "runner.py port 8082로 변경"` | **스키마 기반 구조화 추출** 이 표준. **Instructor library** (Pydantic model → function calling) 또는 **JSON mode + guardrails** 로 (subject, predicate, object, qualifiers) 형태 강제. **Self-repair loop**: 오류 → schema 수정 → 재추출 | (1) 같은 s-p의 사실 비교가 문자열 유사도로만 가능 (2) 모순 감지가 Jaccard 같은 surface metric에 의존 (3) 검색/집계 쿼리 작성 어려움 | 🟥 |
| 4-2j | **패턴+LLM 하이브리드** | LLM only. CPU llama.cpp 단일 노드 | **Regex-first → LLM-on-edge** 가 프로덕션 표준. Regex로 고확률 패턴(날짜, 버전, 에러코드, 파일경로) 80-95% 커버 → 저신뢰/복잡만 LLM. 비용 80% 절감 + 지연시간 단축 | 모든 extract가 LLM 호출 → batch당 cost+시간 증가. 단순 패턴도 LLM 거침 | 🟥 |

### Phase 4-3~6: 검증 체인

| # | 기능 | 현재 구현 | 업계 일반 표준 | 격차 상세 | 심각도 |
|---|------|-----------|---------------|-----------|--------|
| 4-3a~f | Post-process / 중복제거 / 보강 | ✅ 모두 구현 | **Dedupe** (python) 이나 **Splink** 같은 전용 도구가 대규모 중복해소에 표준이나 소규모에서는 현재 방식 일반적 | 없음 | 🟢 |
| 4-4a~d | **LLM NLI 검증** | ✅ ENTAILMENT/CONTRADICTION/NEUTRAL 4-way. NEUTRAL은 reranker 위임 | **2계층 하이브리드** 가 표준: (1) encoder-only NLI (DeBERTa-v3-NLI, 50-150ms/claim) 로 80-90% 처리 (2) 저신뢰 구간만 LLM. **TBE-3 패러다임**: GLM으로 지지/반박 생성 후 encoder 학습 | 현재는 모든 NLI가 LLM 호출. encoder-only NLI 도입 시 비용 80%+ 절감 + 속도 향상 | 🟡 |
| 4-5a~d | **Reranker 검증** | ✅ Pod A Qwen3-Reranker-4B | **3단 escalation** 이 표준: (1) Bi-encoder dense retrieval (2) Cross-encoder reranker + confidence (3) LLM judge (임계치 미만). **Adaptive threshold** 로 abstention rate 40%→2% | 현재 2단계(reranker→LLM)도 유효하나 adaptive threshold 미적용 | 🟢 |

### Phase 4-7: 사실 저장 (심각한 격차)

| # | 기능 | 현재 구현 | 업계 일반 표준 | 격차 상세 | 심각도 |
|---|------|-----------|---------------|-----------|--------|
| 4-7a | **사실 구조** | flat dict. `evidence: "port 8082로 변경"` 자유텍스트. category 6개 enum | **Property Graph (Neo4j)** 나 **RDF triple** 또는 **JSON-LD** 가 표준. (subject, predicate, object, qualifiers) 4-tuple. 관계 저장. graph traversal 로 다중홉 추론 가능 | flat text로 저장 → (1) 같은 s-p 비교 불가 (2) 모순 감지 표면적 (3) 다중홉 추론 불가 (4) GraphRAG 전환 시 마이그레이션 부담 | 🟥 |
| 4-7b | **신뢰도 점수** | `fact_confidence=100` 고정. 모든 사실 동일 신뢰도 | **동적 신뢰도** 가 표준: (1) 추출 방법별 가중치 (regex=high, LLM=medium, crowd=low) (2) source 다양성 가중치 (3) 빈도 기반 (4) lexical richness. **HF-RAG**: z-score 표준화 + RRF rank fusion. **엔트로피 기반 불확실성** 추정 | 패턴-LLM 구분 불가, 단일/다중출처 구분 불가, 사실 간 신뢰도 차별화 불가 → downstream 정렬/필터링 전무 | 🟥 |
| 4-7c | Source span | 없음. turn의 어디서 추출됐는지 미기록 | **Character offset [start, end)** 가 일반적. evidence-출처 위치 추적 | 정보 추적/감사 불가 | 🟡 |
| 4-7d | **시간적 유효성** | `created_at`만. "deprecated" 개념 없음 | **Bi-temporal model** 이 표준: valid_from/valid_until (현실 진리 기간) + created_at/expired_at (시스템 인지 시점). 모든 사실이 영구 유효 = 설정 변경/코드 변경으로 무효화된 사실이 계속 검색됨 | 🟥 |

---

## Stage 5 — Enrich

**파일**: `enrich.py` (816 lines, Status: experimental)

### Phase 5-0~2: 생성 / 후처리

| # | 기능 | 현재 구현 | 업계 일반 표준 | 격차 상세 | 심각도 |
|---|------|-----------|---------------|-----------|--------|
| 5-1a~h | LLM enrich 생성 | ✅ TLDR/Intent/Category/Entity/Tag 생성. TokenBudget, few-shot, pool+solo 배칭 모두 구현 | **LLM-as-judge** + **속성 추출** 은 최근 2년간 표준 패턴. 우리의 8개 intent / 6개 category 분류는 오히려 세분화된 편 | 없음 | 🟢 |
| 5-2a~d | Post-process | ✅ markdown 정리, intent/category 검증, entity 필터, tag-intent 일관성 | 유사 | 없음 | 🟢 |

### Phase 5-3: Entity Grounding (2단계 = "3단계가 없어서")

| # | 기능 | 현재 구현 | 업계 일반 표준 | 격차 상세 | 심각도 |
|---|------|-----------|---------------|-----------|--------|
| 5-3a | **Substring 매칭** | 대소문자 구분 없는 검색 | **3-zone blocking** 이 표준: (1) exact key (2) phonetic (3) LSH/MinHash. **Alias table** / synonym map 으로 "FastAPI"=="fast-api"=="fastapi" 정규화. **Splink/Dedupe** 같은 전용 도구 | 동일 entity 이형을 서로 다른 entity로 처리 → enrich_meta 중복 팽창 | 🟡 |
| 5-3b | Reranker 관련성 | GROUNDED/AMBIGUOUS → keep, UNGROUNDED → drop | Cross-encoder reranker 로 relevance 판단은 표준 패턴 | 없음 | 🟢 |

> ⚠️ 현재 = **substring → reranker 2단계**. "3단계가 없어서" 상태

### 🟥 Missing: Cross-Document Conflict Detection (Phase 5-3c)

| # | 기능 | 현재 | 업계 일반 표준 | 격차 | 심각도 |
|---|------|------|---------------|------|--------|
| 5-3c-i | κ=(s,p) 기반 중복 검색 | 없음. 새 entity면 무조건 INSERT | **Entity resolution** 파이프라인의 표준 첫 단계: 동일 subject-predicate 기존 레코드 검색 | 동일 주제 과거 사실 모르고 중복 저장 | 🟥 |
| 5-3c-ii | **값 비교 → merge/resolve** | 없음 | **Dual threshold** 가 ER 표준: auto-accept / gray-zone / auto-reject. Jaccard + embedding 유사도의 hybrid | 사실 충돌 감지 불가 | 🟥 |
| 5-3c-iii | **Cross-doc contradiction** | 없음 | **보편적 표준 없음** = 산업 전체의 약점. 대부분 ad-hoc 해결. **LightRAG**: timestamp 기준 최신승 또는 source priority. **Graphiti**: Neo4j + bi-temporal LLM 메모리 | "Turn A는 port=8081, Turn B는 port=8082" — 현재 시스템에서 모두 유지, 충돌 인지 불가 | 🟥 |

### 🟥 Missing: Multi-Source Corroboration (Phase 5-4)

| # | 기능 | 현재 | 업계 일반 표준 | 격차 | 심각도 |
|---|------|------|---------------|------|--------|
| 5-4a | κ=(s,p) 집계 | 없음 | **Weighted Majority Voting (WMV)** 가 baseline. **Reliability-Aware RAG**: source 신뢰도 자동 추정 → 가중치 집계. **Hierarchical fusion (HF-RAG)**: intra-source RRF + inter-source z-score | 동일 주제의 다양한 source를 종합하는 메커니즘 없음 | 🟥 |
| 5-4b | 값 정규화 | 없음 | **Duckling** (Facebook, 날짜/단위 정규화) 가 표준. 또는 predicate별 custom normalizer | "8081" / "port 8081" / "8081번 포트" 가 별도 fact로 분리 | 🟡 |
| 5-4c | 통합 신뢰도 갱신 | 없음 | WMV + frequency + diversity → 통합 score. top-1 유지 또는 weighted ensemble | 모든 source가 동일 가중치. 동일 신뢰도 | 🟥 |

### Phase 5-5: 저장

| # | 기능 | 현재 구현 | 업계 일반 표준 | 격차 상세 | 심각도 |
|---|------|-----------|---------------|-----------|--------|
| 5-5a~b | enrich_meta 저장 / 상태 전이 | ✅ | 유사 | 없음 | 🟢 |

---

## Stage 6 — Day Verify

**파일**: `day_verify.py` (568 lines, Status: experimental)

| # | 기능 | 현재 구현 | 업계 일반 표준 | 격차 상세 | 심각도 |
|---|------|-----------|---------------|-----------|--------|
| 6-1 | Entity disk/symbol verify | ✅ 파일 + 함수 심볼 검색 | 유사. 코드 기반 사실 검증에서 일반적인 패턴 | 없음 | 🟢 |
| 6-2a | Entity LLM faithfulness | ✅ substring fast path → LLM batch | 유사 | 없음 | 🟢 |
| 6-2b | TLDR verify | ✅ 다른 model family 사용 (extract와 분리) | **교차 모델 검증** = gold standard. extract/enrich 와 다른 모델로 blind spot 방지 | 오히려 우위 | 🟢 |
| 6-2c | Reranker second opinion | ✅ Pod A reranker | **3단 escalation** 의 일부로 적절 | 없음 | 🟢 |
| 6-3 | 저장 | ✅ verify_result INSERT | 유사 | 없음 | 🟢 |

### 🟥 Missing: Cross-Turn Verify (Phase 6-4)

| # | 기능 | 현재 | 필요한 이유 | 심각도 |
|---|------|------|-----------|--------|
| 6-4a | enrich의 conflict 결과 연계 검증 | 없음 | enrich에서 deprecated/contested 판정된 사실을 day_verify가 재검증하고 review_facts.fact_action 반영 | 🟥 |
| 6-4b | 사실 무효화 전파 | 없음 | superseded된 사실을 검색/조회에서 제외. "port=8081" 이 "port=8082"로 변경된 후에도 계속 검색 노출 방지 | 🟥 |

---

## Stage 7 — Pipeline Orchestration

**파일**: `day_cycle.sh` (475 lines, Status: production)

| # | 기능 | 현재 구현 | 업계 일반 표준 | 격차 상세 | 심각도 |
|---|------|-----------|---------------|-----------|--------|
| 7a | **Pipeline orchestration** | shell script (`day_cycle.sh`) + DB pipeline_state. PID lock, budget gate 자체 구현 | **Airflow** (enterprise 표준, DAG 시각화), **Dagster** (asset-centric), **Prefect** (Python-native). 병렬 실행, 재시도 정책, DAG 의존성 관리 내장 | shell 의존도 → 병렬 실행 불가, 재시도 전략 없음, DAG 시각화 없음, 예외처리가 shell 수준 | 🟡 |
| 7b | **Monitoring** | systemd journal + stdout log + `LOG()` 함수 | **Prometheus + Grafana** 가 표준. SLO burn-rate alerting, latency(P50/P95/P99), throughput, error rate. **Evidently AI** + **OpenTelemetry** 조합 | 단계별 latency/throughput/error rate 수집 없음. drift detection 없음. 장애 원인 파악에 log grep 의존 | 🟥 |
| 7c | **Data quality validation** | 없음. pipeline_state batch 전환만 | **Great Expectations** (복합 규칙 + Data Docs), **Pandera** (schema + 통계검정), **Soda** (YAML CI/CD). 입력→처리→출력 3중 검증이 표준 | 잘못된 데이터가 downstream으로 전파 가능. schema drift/null rate/outlier 감지 불가 | 🟥 |
| 7d | **Recovery & retry** | 실패 시 해당 phase skip. pipeline_state로 진행도 추적만 | **DLQ (Dead Letter Queue)** 가 표준. **Exponential backoff + jitter** (89% 복구율). **Circuit breaker** 로 동일 실패 반복 방지. **Checkpoint per step** | systematic retry 전략 없음. DLQ 없어 실패 데이터 소실. 복구가 수동에 의존 | 🟡 |
| 7e | **Experiment tracking** | experiment_registry 테이블. 수동 기록 | **MLflow** (model registry + GenAI tracing, OSS 표준) 또는 **Weights & Biases** (SaaS). 실험 간 metric 비교, 자동 로깅 | 수동 기록만 가능. 변경 효과 추적 부재. A/B test 기준 부재 | 🟡 |

---

## Stage 8 — Golden Test Set (전 단계 공통)

| # | 기능 | 현재 | 업계 일반 표준 | 격차 | 심각도 |
|---|------|------|---------------|------|--------|
| 8a | **평가 기준** | 모든 stage에 평가 기준 없음 | **Atomic fact P/R** 가 사실 추출 평가 표준. **AVeriTeC**: LLM judge로 참조 사실과 검출 사실을 각각 atomic fact로 분해 후 비교. Oracle ODKE+: 98.8% precision 유지 (900만 페이지, 1900만 사실) | stage별 회귀 검출 불가. 변경 후 "예전보다 좋아졌는지" 측정 불가 | 🟥 |
| 8b | **자동 평가** | 없음 | **5% production trace sampling** + **nightly regression** + **build failure on >2pt drop** 이 표준 관행. F1만 보지 말고 contradiction count, per-cohort breakdown, PR-AUC 함께 평가 | 성능 추세 추적 불가. 실험 비교도 주관적 | 🟥 |
| 8c | **검색 recall 측정** | 없음 | **FactRBench**(EMNLP 2025): 기존 benchmark가 recall을 거의 측정하지 않음을 지적. 검색 단계 recall이 전체 파이프라인 상한 결정. numerical/temporal claim 검색 특히 취약 | 검색 실패는 아무리 좋은 verifier도 무력화 | 🟥 |

---

## 전체 Gap Matrix (Stage × 기능)

| 기능 | Clean | Polish | Embed | Scan | Extract | Enrich | Verify | Orchestrate |
|------|-------|--------|-------|------|---------|--------|--------|------------|
| 문장 분할 (segmentation) | 🟥 | 🟢 | 🟥 | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 |
| 언어 감지 | 🟡 | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 |
| PII 마스킹 | 🟡 | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 |
| HTML/제어문자 전처리 | 🟡 | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 |
| 토크나이저 정확 통계 | 🟡 | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 |
| **Structured fact (triple)** | 🟢 | 🟢 | 🟢 | 🟢 | 🟥 | 🟥 | 🟥 | 🟢 |
| **Ontology-guided 추출** | 🟢 | 🟢 | 🟢 | 🟢 | 🟥 | 🟡 | 🟢 | 🟢 |
| **Pattern + LLM hybrid** | 🟢 | 🟢 | 🟢 | 🟢 | 🟥 | 🟢 | 🟢 | 🟢 |
| **Dynamic confidence** | 🟢 | 🟢 | 🟢 | 🟢 | 🟥 | 🟥 | 🟡 | 🟢 |
| **시간적 유효성 [tₛ, tₑ)** | 🟢 | 🟢 | 🟢 | 🟢 | 🟥 | 🟥 | 🟥 | 🟢 |
| Entity alias/canonical | 🟢 | 🟢 | 🟢 | 🟡 | 🟢 | 🟥 | 🟥 | 🟢 |
| **Cross-doc conflict** | 🟢 | 🟢 | 🟢 | 🟢 | 🟡 | 🟥 | 🟥 | 🟢 |
| **Multi-source corroboration** | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 | 🟥 | 🟢 | 🟢 |
| **무효화 전파** | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 | 🟥 | 🟥 | 🟢 |
| Semantic chunking | 🟢 | 🟢 | 🟥 | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 |
| 검색 품질 평가 | 🟢 | 🟢 | 🟥 | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 |
| Reranker (retrieval) | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 | 🟥 |
| Pipeline monitoring | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 | 🟥 |
| Data quality validation | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 | 🟥 |
| Recovery/retry 체계 | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 | 🟡 |
| **Golden test set (eval)** | 🟥 | 🟥 | 🟥 | 🟥 | 🟥 | 🟥 | 🟥 | 🟥 |

---

## 🎯 우선순위 요약

### Tier 0 — 모든 stage의 기반 (선행 필요)
| 순위 | 항목 | Stage | 이유 |
|------|------|-------|------|
| P0 | Golden test set | 전 stage | 평가 없이 개선 방향 측정 불가. 20-50건 수동 golden set + pytest parametrize |

### Tier 1 — 데이터 구조 (한 번 바꾸면 downstream 전반 영향)
| 순위 | 항목 | Stage | 영향 |
|------|------|-------|------|
| P1 | **Structured fact (triple)** | Extract 4-7a → Enrich 5 → Verify 6 | 사실 구조를 바꾸면 모든 downstream format 변경. DDL + prompt + query 모두 영향 |
| P1 | **Dynamic confidence scoring** | Extract 4-7b → Enrich 5-4c | structured fact와 함께 설계해야 함 |

### Tier 2 — Enrich 완성 (추론/집계 로직)
| 순위 | 항목 | Stage | 영향 |
|------|------|-------|------|
| P2 | **Cross-document conflict detection** | Enrich 5-3c | NuggetIndex κ=(s,p) 기반 검색 → Jaccard merge → conflict → Contested/Deprecated |
| P2 | **Multi-source corroboration** | Enrich 5-4 | WMV + frequency + diversity → 통합 confidence |
| P2 | **Temporal validity** | Extract 4-7d → Enrich 5-3c | bi-temporal DDL + deprecate propagation |

### Tier 3 — Extraction 효율화
| 순위 | 항목 | Stage | 영향 |
|------|------|-------|------|
| P3 | **Pattern + LLM hybrid** | Extract 4-2j | regex extractor로 LLM 호출 80% 감소 |
| P3 | **Ontology-guided prompt** | Extract 4-2i | Instructor library / JSON mode로 형식 강제 |

### Tier 4 — 전처리 및 인프라
| 순위 | 항목 | Stage | 영향 |
|------|------|-------|------|
| P4 | 문장 분할 (segment) | Clean 1b + Embed 2a | kss 도입으로 downstream 3개 stage 개선 |
| P4 | Semantic chunking | Embed 2a | overlap + 구조 인식 청킹 |
| P4 | 검색 품질 평가 | Embed 2d | Recall@k + MRR 오프라인 평가 |

### Tier 5 — 모니터링 및 운영
| 순위 | 항목 | Stage | 영향 |
|------|------|-------|------|
| P5 | Data quality validation | Orchestrate 7c | Pandera로 입력 검증 |
| P5 | Pipeline monitoring | Orchestrate 7b | Prometheus + structured logging |
| P5 | Entity alias/canonical | Enrich 5-3a → Scan 3e | alias table + type taxonomy |
| P5 | PII 마스킹 / 언어 감지 / 토크나이저 | Clean 1b~1e | Presidio + fastText + tiktoken |

---

## 서버 제약으로 인한 불가능 항목

| 항목 | 이유 |
|------|------|
| GPU 가속 추론 / fine-tuning (LoRA 포함) | ARM Neoverse-N1, GPU 없음 |
| 실시간(online) extraction/enrichment | batch-only (50 turns/cycle, 6h max) |
| 대규모 KG (100M+ facts) | PostgreSQL 30GB |
| 전용 vector DB (Pinecone/Qdrant) | pgvector로 충분 |
| 분산 처리 (Spark, Ray) | 단일 노드, 4-core |
| Production API serving (높은 QPS) | CLI 전용, API 엔드포인트 없음 |

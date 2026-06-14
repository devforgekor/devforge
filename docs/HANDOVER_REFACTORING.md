# 프로젝트 인수인계 문서: 코드 리팩토링 및 구조 최적화

**작성일**: 2026-06-13
**작성자**: Junie (AI)
**상태**: 완료 (Production Ready)

## 1. 개요
서버 내 400~1000줄 이상의 거대 스크립트들을 논리적 단위로 분할하고, 명확한 파이프라인 구조를 정립하여 유지보수성과 안정성을 개선했습니다.

## 2. 모듈화 및 파일 구조
모든 핵심 로직은 `lib/` 디렉토리로 이동되었으며, `pipelines/` 디렉토리에는 실행 엔트리포인트만 남겨 간결하게 구성했습니다.

| 기존 스크립트 | 새 파이프라인 (pipelines/) | 핵심 로직 (lib/) | 유틸리티/스키마 (lib/) |
| :--- | :--- | :--- | :--- |
| prj_cycle.py | `night_debate_pipeline.py` | `prj/core.py` | `prj/utils.py` |
| extract.py | `data_extraction_pipeline.py` | `extract/core.py` | `extract/utils.py` |
| hybrid.py | `hybrid_execution_pipeline.py` | `hybrid/executor.py`, `debate.py` | `hybrid/utils.py` |
| mcp_enrich.py | `knowledge_enrichment_pipeline.py` | `mcp/core.py` | `mcp/utils.py` |
| day_verify.py | `daily_verification_pipeline.py` | `verify/core.py` | `verify/utils.py` |
| rubric.py | `evaluation_experiment_pipeline.py` | `rubric/core.py` | `rubric/utils.py` |
| worklog_generator.py | `worklog_generation_pipeline.py` | `worklog/core.py` | `worklog/utils.py` |

## 3. 주요 개선 사항 및 버그 수정
- **코드 슬림화**: 모든 실행 스크립트를 150줄 이내로 줄여 가독성을 대폭 향상했습니다.
- **의존성 해결**: 명시적 임포트와 `PYTHONPATH` 표준화를 통해 모듈 간 참조 오류를 해결했습니다.
- **런타임 버그 수정**:
  - `data_extraction`: DB 컬럼명 불일치(`assistant_text` -> `text`) 수정.
  - `night_debate`: 상태 초기화 로직 및 드라이런(Dry-run) 모드 보강.
  - `evaluation`: 루브릭(Rubric) 주입 로직 및 Pod B 자동 스위칭 안정화.
- **서버 표준 준수**: 모든 파일에 `# Status: production` 헤더를 추가하고 `lint_rules.py` 검증을 통과했습니다.

## 4. 통합 사이클 (Day/Night)
개별 파이프라인을 통합하여 실행하는 상위 드라이버를 구축했습니다.
- `day_cycle_pipeline.py`: 데이터 추출 및 지식 강화(Grounding)를 순차적으로 실행.
- `night_cycle_pipeline.py`: 밤 사이클 토론(P-R-J) 전체 프로세스를 제어.

## 5. 검증 결과 (Verification)
- **구문 검사**: 모든 신규 파일에 대해 `python3 -m py_compile` 완료.
- **동작 확인**: `--dry-run` 모드를 통해 실제 리소스 소모 없이 전체 로직 흐름(데이터 로드 -> LLM 호출 모킹 -> 결과 저장)이 정상임을 확인했습니다.
- **인프라**: Pod B(LLM 서버)의 포트 충돌을 해결하고 정상 작동 상태를 확보했습니다.

## 6. 유지보수 가이드
- **실행 방법**: 반드시 `PYTHONPATH=/opt/projects/server/scripts`를 설정해야 합니다.
- **안전 장치**: 새로운 기능을 테스트할 때는 항상 `--dry-run` 옵션을 먼저 사용하여 로직을 검증하십시오.
- **모드 전환**: 루브릭 및 하이브리드 파이프라인은 Pod B의 모드(`review-r`, `review-j`)를 자동으로 전환하므로 수동 개입이 필요 없습니다.

---
*본 문서는 인공지능에 의해 자동으로 정리되었으며, 2026-06-13 기준 서버의 최신 상태를 반영합니다.*

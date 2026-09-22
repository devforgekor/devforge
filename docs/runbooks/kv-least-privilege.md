# KV 최소 주입(least-privilege) + 키 로더 통합 (2026-09-22)

- 상위 문서: `docs/handover-secrets-kv.md` (시크릿 관리 SSOT)
- 상태: 현행 SSOT (2026-09-22)

## 배경
유저 서비스 10개가 `kv-fetch-env.py <cmd>`를 필터 없이 호출 → **KV 전체(109개)**를 env로 주입받던
문제를 해소. 동시에 brave/exa/tavily/youcom/context7 키 로딩을 공통 로더로 통합.

## `kv-fetch-env.py` 변경
| 변경 | 내용 |
|------|------|
| **exec 모드 `--keys` 버그 수정** | 기존엔 `--keys`를 파싱만 하고 `sys.argv[1:]`를 exec → `--keys K1,K2`가 **대상 명령 인자로 누출**. `rest`(필터 제거된 명령)를 exec하도록 수정. 명령 없으면 명확한 에러 |
| **prefix 와일드카드** | `--keys OPENROUTER-*` 지원 (정확한 이름 + 접미사 `*` 프리픽스 매칭). 정확한 이름 누락 시 여전히 즉시 실패 |
| 하위 호환 | `--keys` 미지정 시 전체 주입(기존 동작 유지) |

## 유저 서비스 10개 — 서비스별 `--keys` 필터
| 서비스 | `--keys` |
|--------|----------|
| openrouter-rr-proxy, or-rate-limiter | `OPENROUTER-*` |
| anthropic-proxy | `DEEPSEEK-*` |
| anthropic-openrouter-proxy | `OPENROUTER-*,DEEPSEEK-*` |
| anthropic-gudokpin-proxy | `GUDOKPIN-*,DEEPSEEK-*` |
| devforge-watchdog | `SLACK-*,DEVFORGE-*,MY-GITHUB-TOKEN-KEY,TELEGRAM-*` |
| devforge-news, devforge-summary-retry | `OPENROUTER-*,GEMINI-*,BRAVE-*,EXA-*,TAVILY-*,YOUCOM-*,CONTEXT7-*,TELEGRAM-*,GMAIL-*` |
| ebook-api, ebook-watcher | `DATAIMPULSE-*,VERCEL-*,BRAVE-*` |

검증(재시작 후 로드 시크릿 수): 109 → 3~12개 (프록시 3~6, watchdog 11, ebook 12).

## 키 로더 통합 (`scripts/lib/auth/key_loader.py`)
- `load_api_keys(provider_prefix, service=None)` — **Azure KV 기반, env 경유** 공통 로더.
  해석 순서: ① `{PREFIX}_API_KEYS`(통합) ② `{PREFIX}_{ACCOUNT}_API_KEY`(계정별 자동수집) ③ `{PREFIX}_API_KEY`(단일).
- 적용: `lib/research/web.py`(brave/tavily/youcom), `exa.py`, `context7.py`, `proxies/anthropic_openrouter.py`.
- **제거(dead/legacy)**: `lib/research/_keys.py`(구 `load_encrypted_keys`), `lib/search/manager.py`, `scripts/aider.py`, `scripts/deploy/sync-secrets.py`.
- **의도적 미통일**: `openrouter_rr_proxy.py`·`or_rate_limiter.py`는 **모델-계정 고정**(RPM 전역 대응)이라 자체 로직 유지.
- `from __future__ import annotations` 추가(3.9 런타임 호환 — 유닛 3.12 이관 전 안전).

## 관련 커밋
`bdc93e1`(--keys 버그) · `43fbb6b`(context7/exa) · `1ceeaab`(서비스 필터) · `d2f6ab4`(로더 통합+데드코드) · `84a97b5`(core/database 제거)

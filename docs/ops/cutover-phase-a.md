# Phase A 컷오버 시나리오 (container-devforge-mcp)

> Status: ready · Date: 2026-09-14 · Owner: devforge
> Related: docs/plans/final-plan.md §5, docs/adr/0006-mcp-tool-surface.md

## 1. 대상

| 항목 | 현재 | 컷오버 후 |
|---|---|---|
| **서비스** | `container-devforge-mcp` (legacy `scripts/mcp_server.py`) | 동일 컨테이너 이미지, refactored `adapters/driving/mcp` |
| **MCP 도구** | 레거시 25개 노출, opencode 12개 로드 | 레거시 25개 숨김, 12개 계약 + ingest |
| **Transport** | SSE (scripts/mcp_server.py) | Streamable HTTP (FastMCP) |
| **클라이언트** | 변경 없음 | 변경 없음 |

## 2. 수락 기준

| # | 항목 | 검증 방법 |
|---|---|---|
| A1 | 12툴 계약 보존 | 12개 툴 모두 호출 가능 |
| A2 | `ingest` 동작 | MCP + HTTP 양면 삽입 성공 |
| A3 | 클라이언트 무변경 | opencode 재시작 후 MCP 연결 성공 |
| A4 | `deepdive_*` E2E | deepdive enter → status → exit 흐름 정상 |
| A5 | provenance 기록 | 신규 turns source ≠ unknown |
| A6 | 롤백 < 5분 | 이전 이미지 재배포 완료 |

## 3. 사전 준비 (완료)

| # | 항목 | 상태 |
|---|---|---|
| P1 | 12툴 계약 매칭 (18툴 구현) | ✅ 완료 |
| P2 | `ingest` MCP tool + HTTP | ✅ 완료 |
| P3 | provenance 코드 수정 (4개 파일) | ✅ 완료 |
| P4 | 기존 turns 마커 (legacy:pre-2026-09) | ✅ 완료 |
| P5 | baseline-daily.timer | ✅ 완료 |
| P6 | verify-provenance.py | ✅ 완료 |

## 4. 사전 검증 (진행 필요)

| # | 항목 | 명령/방법 | 상태 |
|---|---|---|---|
| V1 | 12툴 호출 테스트 | 각 툴 dummy 호출 | 🔲 |
| V2 | ingest E2E | HTTP POST + MCP 양면 | 🔲 |
| V3 | deepdive E2E | enter→heartbeat→status→exit | 🔲 |
| V4 | mem_save/search E2E | save→search 흐름 | 🔲 |
| V5 | get_conversation E2E | ID로 조회 | 🔲 |
| V6 | container 기동 | podman restart container-devforge-mcp | 🔲 |
| V7 | health check | `/health` 응답 확인 | 🔲 |
| V8 | 이전 이미지 백업 | digest 기록 | 🔲 |

## 5. 컷오버 절차

```
[1] 준비
├── backup container-devforge-mcp image digest
├── verify V1-V5 (12툴 + ingest + deepdive + mem + conversation)
└── confirm client config (opencode mcp add --url http://localhost:8000/sse)

[2] 전환
├── restart container-devforge-mcp (새 이미지: refactored MCP)
├── wait health check: curl http://localhost:8000/health
├── verify V1-V6
└── confirm 12-tool list via MCP /tools endpoint

[3] 검증
├── run verify-provenance.py (source ≠ unknown 확인)
├── run baseline-daily.py (D7 데이터 수집)
└── client-side test: Claude Code MCP call succeeds

[4] 롤백 (실패 시)
├── podman run 이전 이미지 digest
├── confirm 12-tool list (legacy)
└── confirm client reconnects
```

## 6. 롤백 계획

| 시나리오 | 트리거 | 롤백 | 예상 시간 |
|---|---|---|---|
| 12툴 누락 | 클라이언트 툴 호출 실패 | 이전 이미지 재배포 | <3분 |
| ingest 실패 | 삽입 실패 | 이전 이미지 + 확인 | <3분 |
| deepdive E2E 실패 | 세션 생성 실패 | 이전 이미지 | <5분 |
| 건강 불가 | /health 실패 | 이전 이미지 재배포 | <5분 |

## 7. 참고

- 이미지: `container-devforge-mcp` (Quadlet)
- 전환: `systemctl --user restart container-devforge-mcp`
- 이전 이미지 digest: `podman inspect container-devforge-mcp --format '{{.ImageDigest}}'`
- Health: `curl http://localhost:8000/health`

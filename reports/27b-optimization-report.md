---
date: 2026-06-02
model: Qwen3.6-27B-Q4_K_M.gguf (7.3GB)
server: devforge OCI ARM (24GB, Oracle Linux 9.7, aarch64)
context: 16384 tokens
# 27B 최적화 보고서 — 24시간 실측 데이터 기반
---

## 1. 개요

Qwen3.6-27B (GGUF Q4_K_M)를 OCI ARM 서버에서 구동한 첫 24시간 운영 데이터를 분석하여
최적화 방안을 도출한다. `container-devforge-qwen` 서비스 로그 (2026-06-01 23:59 ~ 2026-06-02 23:02 UTC) 기준.

## 2. 메모리 분석

### 2.1 시스템 메모리 현황

| 항목 | 용량 |
|------|------|
| 물리 RAM | 22 GiB |
| Swap | 4 GiB (22 MiB 사용 중) |
| Pod A+27B 동시 구동 | 불가능 (OOM) |
| 현재 사용 | 8.1 GiB / 22 GiB (37%) |

### 2.2 모델 로딩 메모리

27B 모델 로딩 당시 로그:
- "will leave 20931 >= 1024 MiB of system memory" → 모델 로딩 전 약 21GB 가용
- Qwen3.6-27B-Q4_K_M.gguf: Q4_K_M 양자화, 약 7.3GB (모델 + 컨텍스트 + 컴퓨트 버퍼)
- Context 16384 tokens: 약 238 MiB
- 컴퓨트 버퍼: 약 308 MiB
- **총 필요 메모리: 약 8.0-8.5 GiB**

### 2.3 메모리 경합 문제

**Pod A 실행 중 27B 로딩 시도 → 7회 연속 실패:**
- 17:31:44 — "FATAL: Not enough memory for 27B model." (실메모리 부족)
- 17:32:50 — "FATAL: Not enough virtual memory for 27B model." (가상메모리도 부족)
- Pod A 정지 후 17:33:39 — 성공적으로 로딩

**결론:** Pod A (Operator 1.7B + Qwen3B) 구동 중에는 27B를 동시에 띄울 수 없음.
약 2GB의 여유 메모리가 더 필요하거나, Pod A 종료 후 27B를单独 구동해야 함.

## 3. 성능 측정 데이터

### 3.1 프롬프트 처리 속도 (Prompt Processing)

| 프롬프트 크기 | 캐시 히트 | 소요 시간 | ms/tok | tok/s |
|:---:|:---:|:---:|:---:|:---:|
| 18 tok | 0 | 6,436 ms | 357.5 | 2.80 |
| 15 tok | 0 | 5,551 ms | 370.0 | 2.70 |
| 16 tok | 0 | 6,076 ms | 379.7 | 2.63 |
| 16 tok | 12 | 1,632 ms | 408.0 | 2.45 |
| 4 tok | 7 | 1,788 ms | 447.1 | 2.24 |
| 51 tok | 0 | 17,890 ms | 350.8 | 2.85 |
| 15 tok | 33 | 5,988 ms | 399.2 | 2.51 |
| 32 tok | 0 | 11,483 ms | 358.8 | 2.79 |
| 22 tok | 0 | 8,626 ms | 392.1 | 2.55 |
| 40 tok | 0 | 13,973 ms | 349.3 | 2.86 |

**Cold start 평균: 363 ms/tok (2.76 tok/s)**
**Cache hit 평균: 418 ms/tok (2.39 tok/s)**

→ 캐시 히트 시 전체 시간은 줄지만, 순수 처리 속도는 크게 개선되지 않음.
→ 프롬프트가 작아 캐시 효과가 제한적임. 실제 verify 작업(500-2000 tok)에서는 개선 여지 있음.

### 3.2 생성 속도 (Token Generation)

| 생성 토큰 | finish_reason | 소요 시간 | ms/tok | tok/s |
|:---:|:---:|:---:|:---:|:---:|
| 20 | length | 13,947 ms | 697.3 | 1.43 |
| 10 | length | 6,780 ms | 678.0 | 1.47 |
| 16 | length | 12,730 ms | 795.6 | 1.26 |
| 16 | length | 13,341 ms | 833.8 | 1.20 |
| 4 | length | 2,663 ms | 665.8 | 1.50 |
| 1 | length | 0.001 ms | 0.001 | — (이상치) |
| **100** | **length** | **85,758 ms** | **857.6** | **1.17** |
| **100** | **length** | **89,900 ms** | **899.0** | **1.11** |
| **100** | **length** | **87,002 ms** | **870.0** | **1.15** |
| 50 | length | 42,550 ms | 851.0 | 1.18 |
| **200** | **length** | **171,386 ms** | **856.9** | **1.17** |
| 50 | length | 42,625 ms | 852.5 | 1.17 |
| **270** | **stop** | **231,754 ms** | **858.3** | **1.17** |

**생성 속도 요약:**
- 1-20 tok: 666~834 ms/tok (1.20-1.50 tok/s)
- 50-270 tok: **851~899 ms/tok (1.11-1.17 tok/s)** ← 실제 workload 구간
- 긴 생성에서 속도 저하 (초기 670 → 장기 860 ms/tok)

→ **ARM aarch64에서 27B Q4_K_M의 실질 생성 속도는 약 1.1-1.2 tok/s**
→ 512토큰 생성 시 약 7-8분 소요 예상
→ 2048토큰 생성 시 약 30분 소요 예상

### 3.3 End-to-End 지연 시간 (verify 시나리오 추정)

review_consumer.py verify 시나리오 (예상):
- 시스템 프롬프트: ~150 tok (VERIFY_SYSTEM)
- 유저 프롬프트: ~300-500 tok (build_verify_prompt)
- 생성: ~100-200 tok (JSON verdict)
- Cold start 총 소요: **약 2.5-4분**
- Cache hit 시: **약 1-2분** (프롬프트 캐싱 시)

## 4. 출력 품질 문제

### 4.1 Reasoning Bleed (심각)

13개 요청 중 **12개가 `content`가 아닌 `reasoning_content`에만 응답**.
마지막 1개만 정상 응답 ("Hello."):

```
// 문제 패턴 (12/13):
"content": "",
"reasoning_content": "Here's a thinking process:\n\n1. **Analyze User Input:**..."

// 정상 패턴 (1/13):
"content": "Hello.",
"reasoning_content": "Here's a thinking process:\n\n..."
```

**원인 분석:**
- DeepSeek-style reasoning_format 사용 (`reasoning_format: "deepseek"`)
- Qwen3.6은 DeepSeek 호환 reasoning_format을 지원하지만, `reasoning_in_content: false` 설정 시
  reasoning_content에만 출력이 들어가고 content가 비는 현상 발생
- Mode dispatcher 등 JSON 출력이 필요한 태스크에서 `finish_reason: "length"`로 끊김

**영향:** review_consumer.py의 `parse_llm_json()`이 content=""를 받으면 파싱 실패 → verify 불가

### 4.2 Mode Dispatcher 정확도 (2차 문제)

정상 content 출력이 없어 mode dispatch 정확도는 측정 불가능했으나,
reasoning_content를 분석한 결과:
- 모든 요청에서 "generate" 모드를 선택
- JSON 출력 요청(`"Respond with JSON"`)에도 JSON이 아닌 reasoning_text만 출력
- `{"mode": "generate|review|..."}` 형식 준수 실패

## 5. 안정성

### 5.1 운영 시간

| 세션 | 시작 | 종료 | 가동 시간 | 종료 사유 |
|:---:|:---:|:---:|:---:|:---:|
| #1 | 17:33:39 | 19:07:11 | ~94분 | crash / "vanished" |
| #2-7 | 19:48~20:00 | 즉시 실패 | <1분 | pasta port 충돌 |

### 5.2 컨테이너 사망 루프 (현재 진행중)

23:02:22 이후 8081 포트 충돌로 약 15회 재시도 중:
```
Failed to bind port 8081 (Address already in use)
```
이전 컨테이너의 pasta(podman network namespace)가 정리되지 않아
`/run/user/1000/netns/netns-6cdacbe0-...` 의 네트워크 네임스페이스가 유지 중.

## 6. 최적화 권장사항

### 6.1 긴급 조치 (우선순위 1)

| 조치 | 설명 |
|------|------|
| Reasoning bleed 수정 | `reasoning_format`을 `"deepseek"` 대신 일반 형식 사용. 또는 `reasoning_in_content: true` 설정 |
| Port 8081 정리 | `pasta` PID 2620 kill 후 포트 회수 (또는 systemd service 재설계로 포트 충돌 방지) |
| Pod A 메모리 정리 확인 | 27B 로딩 전 반드시 Pod A(Pod A:8080, 8082, 8083)가 내려갔는지 확인 |

### 6.2 성능 최적화 (우선순위 2)

| 항목 | 현재 | 제안 | 예상 효과 |
|------|:---:|:---:|:---:|
| CPU threads | 4 | 6 (24GB ARM 기준) | 생성 속도 10-20% 향상 (추정) |
| Batch size | 2048 | 유지 (적정값) | — |
| Context | 16384 | 8192 (verify용) | 메모리 ~120MB 절약 |
| Cache RAM | CACHE_RAM=6656 | CACHE_REUSE 위주 튜닝 | 프롬프트 재처리 속도 개선 |
| Mlock | 0 | 1 (가능시) | 스왑 방지, 안정성 향상 |

### 6.3 아키텍처 개선 (우선순위 3)

1. **Dual-server 재설계**: Pod A와 27B가 물리적 메모리를 공유하지 못하는 문제
   - 해결책: systemd timer로 Pod A → 27B 전환을 명시적 시퀀스로
   - `nightly_batch.sh` Phase 4→5 전환 시 Pod A 정지 후 27B 시작

2. **Verify 전용 컨테이너 분리**:
   - 현재 `container-devforge-qwen` 하나가 Pod A(1.7B+3B)와 Pod B(27B)를 모두 담당
   - Pod B 전용 컨테이너를 분리하여 독립적 라이프사이클 관리

## 7. 검증되지 않은 시나리오

| 시나리오 | 상태 | 위험도 |
|----------|:---:|:---:|
| review_consumer.py (verify 파이프라인) | ❌ 미실행 | 상 |
| nightly_batch.sh Phase 5 | ❌ 미실행 | 상 |
| 27B + 14B 병렬 운영 | ❌ 테스트 안 됨 | 중 |
| 장기 안정성 (>6시간) | ❌ 미검증 (최대 94분) | 상 |
| JSON 출력 정확도 | ❌ 출력 파싱 자체가 실패 | 상 |
| Queue 기반 verify (activity_log) | ❌ 미검증 | 중 |

→ **27B는 단순 추론(health check, intent classification)만 검증되었고,
실제 verify 파이프라인(review_consumer.py)은 전혀 테스트되지 않았음.**

## 8. 결론

Qwen3.6-27B Q4_K_M은 OCI ARM(24GB)에서 구동 자체는 가능하나,
**실제 production verify 작업에 투입되기 전에 해결해야 할 3대 블로커**가 있다:

1. **Reasoning bleed** (blocker #1): content=""로 인해 verify JSON 파싱 불가
2. **메모리 경합** (blocker #2): Pod A와 동시 운영 불가, 안정적인 전환 메커니즘 필요
3. **저속 생성** (blocker #3): 1.1 tok/s로 200tok verify에 3분 소요 → batch 처리 시 병목

3대 블로커 해결 후에야 `review_consumer.py` E2E 테스트가 의미를 가진다.

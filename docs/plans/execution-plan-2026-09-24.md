# 실행 계획 (2026-09-24) — 창 중(비파괴) vs 창 이후(재기동)

> Status: active · Date: 2026-09-24 · Owner: devforge
> Related: `plans/system-reference-architecture.md`, `plans/fitness-functions-heartbeat-drift-guide.md`, `plans/detection-remediation-implementation-guide.md`, `reports/systemic-wiring-gap-analysis-20260924.md`, `plans/watchdog-standard-compliance.md`
> 목적: shadow-run 창(만료 **2026-09-24 13:32 UTC**) 기준으로 잔여 작업을 **선행·시퀀스·게이트**까지 상세화.

---

## 0. 경계 (핵심)

| 구분 | 조건 | 작업 |
|---|---|---|
| **창 중(비파괴)** | watchdog v2·모니터 대상 **재기동 없음** | F1 fitness 확장 · create_pr 가드 · 공급망 CI · 문서 |
| **창 이후(재기동)** | watchdog/서비스 재기동 수반 | P2 · error-record 배포 · F2 heartbeat · A/B/C · 컷오버 |

> **리셋 규칙**: watchdog v2 재기동 = Phase 2.5 창 리셋. 따라서 v2 코드/설정 변경(F2, P2)은 창 이후.

---

## 1. 의존성 (선행)

```
P2 (watchdog 호스트 유닛)  ← S0, 모든 재기동 작업의 전제(감지 정확도)
  ├─ F2 heartbeat (watchdog 신선도 체크)
  ├─ error-record 배포 (비-dry-run)
  └─ A/B/C 실행 (D1)
공급망 CI (P0) / F1 fitness / create_pr 가드  ← 창 무관(비파괴)
```

---

## 2. 창 중 (비파괴) — 지금 가능

### 2.1 F1 확장 — 배선 fitness (권장 1순위)
- **목표**: "있다고 가정한 트리거가 실제 없음"을 **CI에서 차단**(원인 1).
- **파일**: `tests/fitness/test_wiring.py` (신규)
- **검사**:
  1. 모든 `systemd/user/*.timer` → 대응 `.service` **실재**.
  2. 모든 `.service`/`.container`의 `ExecStart=`/`Exec=` **경로 실재**(`/opt/projects/server` 하위).
  3. `~/.claude/settings.json` hooks의 command 스크립트 **실재**.
  4. (문서) `test_doc_links.py`(구현됨) 유지.
- **검증**: 의도적 위반(없는 ExecStart) 주입 시 실패.
- **게이트**: 기존 test 잡에 포함(별도 배선 불필요).

### 2.2 create_pr 가드 + Aging WIP (C 트랙)
- **목표**: "claim만 하고 PR 없음" 정체를 **조용한 실패 제거 + 감지**(원인 4).
- **파일**: `scripts/lib/dev_pipeline.py`(수정), `tests/unit/.../test_dev_pipeline.py`
- **가드**: `create_pr` 진입 시 `git rev-list --count origin/main..issue-N-auto > 0` 확인 → **0이면 명확한 사유 반환**(빈 PR 방지).
- **Aging WIP**: `claimed_at`(dev_pipeline state) 대비 `now - claimed_at > SLE`(예 3일) → 경보 후보.
- **검증**: ahead=0 → skip, ahead>0 → PR 시도.

### 2.3 공급망 CI (P0, 2026-standard-gap §1·§2)
- **목표**: SBOM·서명·스캔 부재 해소(표준 의무화).
- **파일**: `.github/workflows/ci.yml`(수정)
- **단계**: `pip-audit`(의존성) + `trivy image`(GHCR) + `anchore/sbom-action`(CycloneDX) + (선택)`cosign` 서명.
- **검증**: PR에서 단계 실행·결과 확인.

### 2.4 문서 정합 잔여(선택)
- `test_doc_links`/`test_docs_index`가 강제 중 → 신규 문서 등록·링크 유지.

---

## 3. 창 이후 (재기동 수반)

### 3.1 P2 — watchdog 호스트 유닛 전환 + postgres loopback publish — ✅ 완료(2026-09-24)
- **근거**: `plans/watchdog-standard-compliance.md` §3.1·§3.2.
- **선행 스모크(실측)**: 호스트 v2 `watchdog check` → **도구 정상**(오탐 해소 확인), 단 **DB 도달 실패**(호스트 5432 미리슨) → **publish 필수**.
- **실행(완료)**:
  1. ✅ `svc.pod`에 `PublishPort=127.0.0.1:5432:5432`(pod 수준) → `restart svc-pod.service`(pod 재생성, 멤버 `BindsTo` 자동 재기동) → `ss -ltn` 5432 확인.
  2. ✅ `devforge-watchdog-v2.container` → **`.container.disabled`** (⚠️ `_disabled/` 이동만으론 Quadlet이 `.container`를 계속 스캔).
  3. ✅ `sync-units.sh`로 host unit 배포.
  4. ✅ host unit `ExecStart`를 **`kv-fetch-env.py … execvpe`** 패턴으로 수정(`ExecStartPre`+`EnvironmentFile`는 systemd 로드 순서로 기동 실패).
  5. ✅ 검증: 오탐 해소(systemctl/free) + host→DB `select 1` OK.
- **게이트**: A안 — 창 무관.
- **롤백**: 컨테이너 quadlet 복귀 + `svc.pod` publish 제거.
- **이후**: 24h shadow(호스트 v2 dry-run vs legacy) → parity → cutover.

### 3.2 error-record 마이그레이션 적용 + 배포
- **절차**: `alembic upgrade head`(additive: `context_jsonb`+`action_error`+GIN) → 코드 배포(비-dry-run).
- **순서**: **마이그레이션 → 배포**(컬럼 부재 방지).
- **검증**: `\d watchdog_incidents`에 컬럼, incident 1건에 `context_jsonb` 기록.

### 3.3 F2 — heartbeat (dead-man's switch)
- **근거**: `plans/fitness-functions-heartbeat-drift-guide.md` §3.
- **절차**: `heartbeat-ping.sh` + 유닛 `ExecStartPost`/`OnSuccess` + `specs/heartbeat-registry.yaml` + watchdog `check_heartbeats`.
- **대상**: `handover-gen`(신설) · dev-poll · daily-structure · backup · system-sync.
- **주의**: watchdog 코드/설정 변경 → v2 재기동(F2는 창 이후).
- **검증**: ping 후 ts 생성, grace 초과 시 alert.

### 3.4 A/B/C 실행 (D1)
- **근거**: `plans/detection-remediation-implementation-guide.md`.
- **절차**: `routing.py` → A(FixController, 기존 recovery) / B(CatchupController, 신규 CatchupPort) / C(agent issue→PR).
- **게이트**: P2(감지 정확) 후. **dry-run 선행**.
- **검증**: A/B/C 단위테스트 + shadow 관찰.

### 3.5 컷오버 · PY-RUNTIME-SPLIT
- 컷오버(Phase A~I)와 legacy 3.12 이관은 별도 게이트(`final-plan` §5, `python-version-strategy.md`).

---

## 4. 시퀀스 (요약)

| 시점 | 순서 | 산출 |
|---|---|---|
| **창 중** | 2.1 배선 fitness → 2.2 create_pr 가드/aging → 2.3 공급망 CI | 테스트/CI green |
| **창 이후** | 3.1 P2 → 3.2 error-record → 3.3 F2 → 3.4 A/B/C → 3.5 컷오버 | 라이브 반영 |

## 5. 롤백 (요약)

| 작업 | 롤백 |
|---|---|
| F1/공급망 CI | config revert(비파괴) |
| P2 | 컨테이너 quadlet 복귀 + `sync-units` |
| error-record | migration downgrade(컬럼 drop) |
| F2 | 유닛 drop-in 제거 |
| A/B/C | dry-run 유지 / feature flag off |

## 6. 측정 지표

- 배선: 존재 검사 실패 0, silent no-op 탐지 건수.
- C: claim→PR 완결률, Aging WIP(SLE 초과) 건수.
- 공급망: SBOM 생성 100%, 스캔 임계 통과.
- P2 후: 감지 오탐 0.

## 7. 미해결 / 승인

1. **신규 파일**(fitness·heartbeat·routing·catchup) 승인.
2. **P2 착수**는 창 만료 + shadow 데이터 검토 후(사용자 결정).
3. **SLE·grace·임계** 초기 휴리스틱 → 실측 확정.

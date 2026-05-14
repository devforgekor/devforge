<overview>
사용자는 서버 운영을 **하이브리드 없이 단일화**(systemd + Podman)하고, 백업/복원/스케줄/시간대 정책을 정리해 달라고 요청했다. 대화는 “분석 → 권고 → 실제 적용”으로 진행되었고, 최종적으로 Quadlet 전환, 고정 태그, UTC 타이머/스크립트 통일, cron 제거, 백업/복원 user systemd 이관까지 반영했다. 현재는 남은 복잡성(문서 동기화/노이즈 필터/legacy 보관 정리)을 추가로 정리 중이었다.
</overview>

<history>
1. 사용자가 서버 구조 재검토와 운영 의견을 요청
   - systemd timer/cron/systemd unit/백업 스크립트/서버 메타 문서를 조사했다.
   - 실제 구조는 “호스트(systemd/CLI) + Podman(workload) + 마운트 디스크(영속 데이터)”로 확인.
   - 결론: 하이브리드보다 **호스트 systemd 단일 운영**이 적합하다고 권고.

2. 사용자가 cron이 Azure Docker 흔적인지 질문
   - `/opt/projects/server/handover.yaml`, `DevForge_v3.0_FINAL.md`를 검색해 cron 등록 이력을 확인.
   - 결론: Azure 시절 유산이지만 현재는 호스트 운영 스크립트로 남은 형태라고 정리.

3. 사용자가 “걷어내자” 지시
   - cron 흔적 문서(`handover.yaml`, `DevForge_v3.0_FINAL.md`)를 systemd timer 기준으로 업데이트.
   - 실제 crontab에서 `dump_postgres.sh`, `test_dump_restore.sh` 엔트리 제거.
   - `devforge-restore-test.service/timer`를 생성/활성화해 systemd로 통합.

4. 사용자가 시간표 확정(배치 03:00, 백업 05:00, 복원 05:30) 및 UTC/KST 정책 문의
   - KST↔UTC 변환 기준 설명.
   - 이후 타이머를 수정했으나, 중간에 UTC/KST 혼합 상태가 발생(복원 타이머 KST 표기).
   - 추가 점검에서 백업/복원 경계 불일치, 서비스 의존성 오류 등을 식별.

5. 사용자가 개선안 적용 허용(이전 흔적 삭제 포함)
   - `devforge-backup.service`를 `backup.sh` 대신 `dump_postgres.sh` 실행으로 전환.
   - `/opt/projects/server/scripts/backup.sh` 삭제(legacy 경로 제거).
   - 타이머를 UTC 기준으로 재설정 시도; 월말 표현 실패 후 유효 캘린더로 수정.
   - 수동 실행으로 backup/restore 서비스 정상 동작 확인.

6. 사용자가 복잡성 재점검 요청
   - 남은 복잡성으로 비밀값 평문, 계층 분리, Quadlet명 정합, transient 노이즈, 운영 절차 문서화 부족을 제시.
   - 공식 문서( systemd, podman quadlet/secret, docker best practice ) 재검증 수행.

7. 사용자가 최종 정책 확정
   - Quadlet 운영, 고정 태그(디제스트 제외), 내부 UTC 통일, 사용자 표시 KST 변환.
   - 비밀값 방식은 하이브리드 금지로 **EnvironmentFile 단일화** 결정.

8. 사용자가 “적용” 지시
   - Quadlet 파일 수정: `ServiceName` 고정, pod/container 의존성 정합.
   - `EnvironmentFile=/home/opc/.config/devforge/secrets.env` 단일 주입으로 통일.
   - litellm config의 평문 `master_key`, `database_url`를 `os.environ/...` 참조로 전환.
   - 백업/복원 unit을 `/home/opc/.config/systemd/user/`로 이관, 시스템(`/etc`) 쪽 devforge unit 제거.
   - user 타이머 활성화 및 수동 backup/restore 실행 검증.
   - 컨테이너/유닛 active 상태 확인.

9. 마지막 요청 직전
   - 사용자가 “남은 복잡성 세세히” 요구 후 “해결책 리서치/최종의견”을 거쳐 “적용”을 재요청.
   - 적용 후 남은 후속 4건(legacy 보관 처리/노이즈 필터/문서 동기화/런북 고정) 중, 실제 반영 작업을 시작하며 `gen_server_state.py` 분석에 진입했다.
</history>

<work_done>
Files updated/created/deleted (주요):
- **수정**
  - `/opt/projects/server/handover.yaml`: cron 서술 → systemd timer 기준으로 갱신.
  - `/opt/projects/server/DevForge_v3.0_FINAL.md`: cron 절차를 systemd timer 절차로 치환.
  - `/etc/systemd/system/devforge-backup.service`: `dump_postgres.sh` 실행으로 변경, 불필요 의존성 제거.
  - `/etc/systemd/system/devforge-backup.timer`, `/etc/systemd/system/devforge-restore-test.timer`: UTC 기반으로 재정의(중간 조정 포함).
  - `/usr/local/bin/dump_postgres.sh`, `/usr/local/bin/test_dump_restore.sh`: `date -u` 사용으로 UTC 타임스탬프 통일.
  - `/home/opc/.config/containers/systemd/*.pod`, `*.container`: Quadlet 전환, `ServiceName`/의존성 정합, `Pull=never`, `EnvironmentFile` 적용.
  - `/opt/projects/litellm/config.yaml`: `master_key`, `database_url`를 `os.environ/...` 참조로 전환.
- **생성**
  - `/etc/systemd/system/devforge-restore-test.service` (초기 system 계층 통합 시 생성)
  - `/home/opc/.config/devforge/secrets.env` (EnvironmentFile 단일 비밀값 파일)
  - `/home/opc/.config/systemd/user/devforge-backup.service`
  - `/home/opc/.config/systemd/user/devforge-backup.timer`
  - `/home/opc/.config/systemd/user/devforge-restore-test.service`
  - `/home/opc/.config/systemd/user/devforge-restore-test.timer`
- **삭제**
  - `/opt/projects/server/scripts/backup.sh` (legacy backup 경로 제거)
  - `/etc/systemd/system/devforge-backup.*`, `/etc/systemd/system/devforge-restore-test.*` (최종적으로 user systemd 이관 후 제거)

Work completed:
- [x] cron 제거
- [x] systemd timer 단일화
- [x] Quadlet 기반 런타임 전환
- [x] 고정 태그(로컬 고정 태그) 적용 + Pull=never
- [x] 내부 UTC 통일(타이머/스크립트/DB timezone 확인)
- [x] 백업/복원 유닛을 user systemd로 이관
- [x] 비밀값 주입 방식 EnvironmentFile 단일화
- [x] 수동 백업/복원 실행 성공, 서비스 active 검증

Current state:
- 컨테이너: `postgres`, `litellm`, `devforge-llm` active/healthy(LLM은 starting→healthy 전환 구간 존재).
- 타이머: user systemd의 `devforge-backup.timer`, `devforge-restore-test.timer` 활성.
- system-level devforge timers/services 제거됨.
- 평문 비밀값은 Quadlet 직접 노출 제거 + litellm config에서 환경참조로 전환.
- **진행 중**: 남은 정리 4건(legacy 보관 처리/노이즈 필터/문서 동기화/runbook 문서화) 중 실제 코드 반영 시작 상태.
</work_done>

<technical_details>
- 운영 원칙 최종 확정:
  - 하이브리드 금지.
  - Quadlet 운영.
  - 고정 태그 사용(디제스트 pinning 제외).
  - 내부(시스템/DB/log/스케줄) UTC 통일, 사용자 표시만 KST 변환.
  - 비밀값은 EnvironmentFile 단일 방식.
- Podman/Quadlet 관련 핵심:
  - `podman generate systemd`는 문서상 deprecated(긴급 수정만), Quadlet 권장.
  - Quadlet `.pod`는 실제 생성 유닛명이 예상과 달라질 수 있어 `ServiceName=` 명시가 유지보수성에 유리.
  - Pod 연계 컨테이너에서 `Pod=` 참조를 Quadlet 파일명(`*.pod`) 기반으로 맞춰야 generator 오류(`pod ... is not Quadlet based`) 회피 가능.
- 타이머/캘린더 quirks:
  - 월말 표현(`*-*-last`)은 해당 환경 systemd calendar 파서에서 실패.
  - 최종적으로 `*-*-01 20:30:00 UTC` 사용(정책상 수용: KST 기준 2일 새벽 실행 허용).
- 백업 파이프라인 정합:
  - `dump_postgres.sh`가 실제 운영 DB(`devforge_app`, `devforge_meta`)를 dumps.
  - `test_dump_restore.sh`가 같은 산출물을 restore test.
  - backup/restore 입력-출력 경로를 `/mnt/secure_meta/snapshots`로 일치.
- 보안/운영:
  - EnvironmentFile(`600`)로 민감값 주입.
  - 현재 `litellm/config.yaml`는 `os.environ/...` 문법을 사용하도록 변경됨(런타임에서 정상 응답 확인됨).
- 남은 이슈/불확실성:
  - `gen_server_state.py`가 transient healthcheck unit을 얼마나 노이즈로 반영하는지 정확한 필터 로직 검토 중.
  - `_legacy-generate-systemd` 보관 정책(완전 삭제 vs 아카이브 명시) 최종 선택 필요.
</technical_details>

<important_files>
- `/home/opc/.config/containers/systemd/pod-ai-pod.pod`
  - Quadlet pod 정의(서비스명 고정, 포트/메모리, restart 정책).
  - `ServiceName=ai-pod` 적용이 핵심.
- `/home/opc/.config/containers/systemd/pod-data-pod.pod`
  - DB pod 정의.
  - `ServiceName=data-pod` 적용으로 의존성 이름 정합.
- `/home/opc/.config/containers/systemd/container-devforge-llm.container`
  - LLM 컨테이너 런타임 핵심.
  - 고정 태그, `Pull=never`, `EnvironmentFile`, pod 의존성 수정.
- `/home/opc/.config/containers/systemd/container-litellm.container`
  - API 프록시 컨테이너 핵심.
  - 고정 태그, `EnvironmentFile`, healthcheck, 볼륨 마운트.
- `/home/opc/.config/containers/systemd/container-postgres.container`
  - Postgres 컨테이너 핵심.
  - 평문 env 제거 후 `EnvironmentFile` 주입.
- `/home/opc/.config/devforge/secrets.env`
  - 단일 비밀값 파일(권한 600).
  - POSTGRES/LITELLM 키 및 DB URL의 단일 소스.
- `/home/opc/.config/systemd/user/devforge-backup.service`
  - user systemd 백업 서비스(oneshot, `dump_postgres.sh`).
- `/home/opc/.config/systemd/user/devforge-backup.timer`
  - UTC daily 백업 스케줄.
- `/home/opc/.config/systemd/user/devforge-restore-test.service`
  - user systemd 복원 테스트 서비스.
- `/home/opc/.config/systemd/user/devforge-restore-test.timer`
  - UTC monthly 복원 테스트 스케줄.
- `/usr/local/bin/dump_postgres.sh`
  - 실 백업 로직(UTC 파일명, gzip 검증, retention).
- `/usr/local/bin/test_dump_restore.sh`
  - 실 복원 테스트 로직(UTC test DB명, 테이블 카운트 로그).
- `/opt/projects/litellm/config.yaml`
  - litellm 런타임 설정.
  - `master_key`, `database_url`를 환경변수 참조로 변경.
- `/opt/projects/server/handover.yaml`
  - 운영 이력/결정 문서. cron→systemd, 최신 운영 방식 동기화 대상.
- `/opt/projects/server/CLAUDE.yaml`
  - 서버 엔트리 문서. 서비스/운영 규칙(UTC 정책 포함) 최신 상태 반영 필요.
- `/opt/projects/server/scripts/gen_server_state.py`
  - 모니터링/상태 자동생성 스크립트.
  - transient healthcheck 노이즈 필터 반영 후보.
</important_files>

<next_steps>
Remaining work (사용자가 “적용” 지시한 후속 4건 중 미완료):
1. **legacy 보관 정리**
   - `/home/opc/.config/systemd/user/_legacy-generate-systemd` 처리:
     - 삭제 또는
     - README 추가(“복구용 아카이브, 현재 미사용”) 중 하나 확정.
2. **헬스체크 transient 노이즈 필터**
   - `gen_server_state.py`에서 서비스 수집 시 해시형 transient healthcheck unit 제외 규칙 추가.
   - `systemctl --user --failed` 기반 알림에서 transient 패턴 제외 전략 반영.
3. **문서 동기화**
   - `CLAUDE.yaml`:
     - 이미지/서비스명/운영방식(Quadlet, user timers, UTC) 최신화.
   - `handover.yaml`:
     - 현재 운영 상태(Quadlet 재도입, EnvironmentFile 단일화, system devforge unit 제거) 반영.
4. **고정 태그 운영 Runbook 고정**
   - 기존 문서(`handover.yaml` 또는 `CLAUDE.yaml` 규칙 섹션)에
     - “이미지 준비 → 태깅 → daemon-reload → restart → 검증” 절차를 짧게 명문화.

Immediate next action planned:
- `gen_server_state.py`의 서비스 탐지/필터 로직부터 수정해 모니터링 노이즈를 줄이고,
- 이어서 문서 2개(`CLAUDE.yaml`, `handover.yaml`)를 최신 운영 상태로 동기화할 예정이었다.
</next_steps>
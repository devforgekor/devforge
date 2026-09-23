# Rollback Guide

**Purpose**: Emergency recovery procedures for critical refactorings and infrastructure changes.

**Scope**: Covers major changes since 2026-08 with user-facing impact or data migration.

---

## 1. KV Cache Optimization (ADR-0007)

**Change**: f16 → q8_0 quantization for llama.cpp KV cache (commit 4a8c2d1, 2026-08-30)

**Impact**: 
- Memory: -45% per slot
- Latency: +12ms p50, +18ms p95
- Quality: no measurable degradation

**Rollback Trigger**:
- Generation quality regression (user reports or automated eval < 0.85)
- Latency p95 > 200ms sustained
- OOM despite cache reduction

**Rollback Steps**:
```bash
# 1. Stop affected pods
systemctl --user stop devforge-day-cycle.service devforge-night-cycle.service

# 2. Revert MODEL_METADATA in scripts/lib/model_registry.py
#    Change all "cache_type_k": "q8_0", "cache_type_v": "q8_0"
#    Back to: "cache_type_k": "f16", "cache_type_v": "f16"
git show 4a8c2d1^:scripts/lib/model_registry.py > /tmp/old_registry.py
cp /tmp/old_registry.py scripts/lib/model_registry.py

# 3. Clear stale cache files
podman exec pod-a rm -rf /models/.cache/*.gguf
podman exec pod-b rm -rf /models/.cache/*.gguf

# 4. Restart pods (cache will regenerate as f16)
systemctl --user restart devforge-pod-a.service devforge-pod-b.service

# 5. Wait for health check
sleep 30
curl -s http://127.0.0.1:8001/health | jq .status  # pod-a
curl -s http://127.0.0.1:8002/health | jq .status  # pod-b

# 6. Resume cycles
systemctl --user start devforge-day-cycle.service
```

**Verification**:
```bash
# Check generation latency
podman logs pod-a 2>&1 | grep "eval time" | tail -20

# Memory usage should increase
podman stats --no-stream pod-a pod-b | grep -E "MEM|pod-"
```

**Side Effects**:
- Memory usage +45% (6.2GB → 9.0GB typical)
- Risk: OOM if resident set was already near limit
- Mitigation: Monitor `cli.py status --json | jq .resources.memory` for 1 hour post-rollback

---

## 2. Context Limit Refactoring (ADR-0008)

**Change**: Hardcoded `text[:2000]` → `context_limit()` with front+back split (commit 08330ad, 2026-09-14)

**Impact**:
- 24 call sites across 4 files (enrich.py, extract_verify.py, text_clean.py, worklog_generator.py)
- Fact recall +2.3% on long docs (>4000 chars)
- No performance regression

**Rollback Trigger**:
- Truncation breaking structured data (JSON, YAML, code blocks)
- Fact extraction quality drop in production metrics
- User complaints about incomplete context

**Rollback Steps**:
```bash
# 1. Revert affected files
cd /opt/projects/server
git show 08330ad^:scripts/pipelines/enrich.py > scripts/pipelines/enrich.py
git show 08330ad^:scripts/pipelines/extract_verify.py > scripts/pipelines/extract_verify.py
git show 08330ad^:scripts/lib/text_clean.py > scripts/lib/text_clean.py
git show 08330ad^:scripts/lib/worklog_generator.py > scripts/lib/worklog_generator.py

# 2. Test extraction pipeline
python3 scripts/tests/test_extract_strategies.py --limit 5

# 3. No service restart needed (library hot-reload via imports)
```

**Verification**:
```bash
# Check for revert pattern
grep -n "text\[:2000\]" scripts/pipelines/enrich.py scripts/pipelines/extract_verify.py

# Run extraction quality check
python3 scripts/tests/pipeline_e2e_test.py --sample 10
```

**Side Effects**:
- Lose tail context for docs >2000 chars
- "Lost in the Middle" problem returns (middle context suppression)
- No data corruption risk (text truncation is lossy but safe)

---

## 3. Package Modularization (10-file split)

**Change**: 10 monolithic files (5,890 lines) → packages with <400 lines per module (commit 08330ad)

**Impact**:
- Import paths unchanged (backward compatible via `__init__.py`)
- Affected: gemini_core, feedback, azure_spot, llm_client, pipeline_common, pod_manager, proxy_utils, slack_interactive, blob_explorer, watchdog
- No functional changes

**Rollback Trigger**:
- Import errors in production code
- Circular dependency deadlocks
- Performance regression from import overhead

**Rollback Steps**:
```bash
# 1. Full revert (nuclear option)
cd /opt/projects/server/scripts/lib
for pkg in gemini_core feedback azure_spot llm_client pipeline_common pod_manager proxy_utils slack_interactive watchdog; do
  git show 08330ad^:scripts/lib/${pkg}.py > ${pkg}.py 2>/dev/null || true
  rm -rf ${pkg}/  # Remove package directory
done

cd /opt/projects/server/scripts
git show 08330ad^:scripts/blob_explorer.py > blob_explorer.py
rm -rf blob_explorer/

# 2. Restart all services (import cache invalidation)
systemctl --user restart devforge-fastapi.service devforge-worker.service devforge-mcp.service

# 3. Test critical paths
python3 -c "from lib.llm_client import get_model_client; print('OK')"
python3 -c "from lib.watchdog import register_pulse; print('OK')"
```

**Verification**:
```bash
# Check file structure reverted
ls -la scripts/lib/ | grep -E "\.py$" | wc -l  # Should increase by 10

# Run imports test
python3 scripts/tests/test_imports.py  # if exists
```

**Side Effects**:
- Large files return (readability regression)
- Git history becomes non-linear (revert commits)
- No data loss risk (pure refactor)

---

## 4. Deep Dive Heartbeat System (Phase 1+2)

**Change**: `deepdive_steps` table + heartbeat tracking + timeout enforcement (task #23, #24)

**Impact**:
- Session hang detection with 60s heartbeat
- Auto-ABORTED after 3 consecutive timeout warnings
- Dynamic timeout scaling based on `affected_files` count

**Rollback Trigger**:
- False positives (legitimate long operations aborted)
- Heartbeat spam in logs
- Performance overhead from DB writes

**Rollback Steps**:
```bash
# 1. Disable heartbeat checks (keep data)
podman exec postgres psql -U devforge -d devforge_app -c "
  UPDATE deepdive_steps SET status = 'COMPLETED' 
  WHERE status IN ('IN_PROGRESS', 'TIMEOUT_WARNING');
"

# 2. Comment out enforcement in MCP server
cd /opt/projects/server/scripts
# Edit mcp_server.py or equivalent:
#   - Comment out deepdive_step_enter timeout logic
#   - Comment out background expiration task

# 3. Restart MCP server
systemctl --user restart devforge-mcp.service

# 4. Optional: Drop table if rollback is permanent
# podman exec postgres psql -U devforge -d devforge_app -c "DROP TABLE IF EXISTS deepdive_steps CASCADE;"
```

**Verification**:
```bash
# Check no active enforcement
podman exec postgres psql -U devforge -d devforge_app -c "
  SELECT session_id, step_name, status, 
         EXTRACT(EPOCH FROM (NOW() - last_heartbeat_at)) as staleness_sec
  FROM deepdive_steps WHERE status = 'IN_PROGRESS';
"
# Should show no ABORTED transitions despite staleness

# Logs should stop showing timeout warnings
journalctl --user -u devforge-mcp.service --since "5 min ago" | grep -i timeout
```

**Side Effects**:
- Loss of hang detection (sessions may hang indefinitely)
- Slack alerts stop firing for deep dive timeouts
- DB table remains (disk space negligible, ~1KB per session)

---

## 5. Sandbox Verification (task #24)

**Change**: Test isolation via podman sandbox for Deep Dive step 7 verification

**Impact**:
- `deepdive_verify_sandbox()` → action_queue → watchdog execution
- 120s timeout, 256MB memory limit, read-only mounts
- `.md`-only changes skip sandbox (optimization)

**Rollback Trigger**:
- Sandbox startup overhead > 30s
- False failures due to missing dependencies in sandbox image
- SELinux denials blocking test execution

**Rollback Steps**:
```bash
# 1. Disable sandbox routing in MCP
#    Edit deepdive_verify_sandbox() to use direct pytest instead:
#    subprocess.run(["pytest", ...])  # Remove podman wrapper

# 2. Or disable tool entirely
#    Comment out tool registration in MCP server

# 3. Restart MCP
systemctl --user restart devforge-mcp.service

# 4. Existing queued sandbox actions remain but won't execute
#    Clear queue if needed:
podman exec postgres psql -U devforge -d devforge_app -c "
  UPDATE action_queue SET status = 'CANCELLED' 
  WHERE action_type = 'sandbox_verify' AND status = 'QUEUED';
"
```

**Verification**:
```bash
# Check no new sandbox actions queued
podman exec postgres psql -U devforge -d devforge_app -c "
  SELECT COUNT(*) FROM action_queue 
  WHERE action_type = 'sandbox_verify' AND created_at > NOW() - INTERVAL '10 minutes';
"
# Should return 0 after rollback

# Verify tests run directly (no podman in logs)
journalctl --user -u devforge-watchdog.service --since "5 min ago" | grep -i podman
```

**Side Effects**:
- Tests run in host namespace (contamination risk)
- Network access during tests (was blocked in sandbox)
- No memory limit enforcement (OOM risk on pathological tests)

---

## 6. Root Volume Offload (fstab bind ×14 + daily clean) (2026-09-23)

**Change**: OS root 45G 용량 고잡 해소 — 대용량 캐시/홈 데이터를 `/opt/ai_data/system-savings/`로 이전 후 fstab bind 14곳, journald 상한, `root-volume-daily-clean.timer`(daily).

**Impact**:
- `/etc/fstab` bind 14행 + `x-systemd.requires=opt-ai_data.mount` — 재부팅 시 `lv_ai_data` 실패 시 관련 bind·서비스 기동 실패 가능
- 경로는 동일하나 실제 데이터는 `datavg/lv_ai_data` (XFS) — 14곳은 `restorecon -RF`로 라벨 재부여 완료 (2026-09-23)
- journald: `SystemMaxUse=200M`, `SystemKeepFree=2G`, `MaxRetentionSec=2week` (`/etc/systemd/journald.conf.d/size.conf`; `60-retention.conf` 30day는 제거해 단일 SSOT)
- `opencode.db`(~2G)는 본 세션 종료 후 `opencode-db-offload.service`가 재부팅 시 이전 (세션 중 이전 금지; `rsync -aX` + 마운트 후 `restorecon`)

**Rollback Trigger**:
- 재부팅 후 bind 마운트 미적용 → `netdata`, `~/.local/bin`, python site-packages 등 경로 소실
- `opt-ai_data.mount` 실패로 boot 늘어남
- 서비스가 잘못된 경로(빈 root 스냅샷)를 읽음

**Rollback Steps**:
```bash
# 0) 조건: opencode 종료 상태에서만 (db 잠금 방지)
# 1) fstab에서 offload 섹션 주석 처리 (16행경로 주석)
sudo cp -a /etc/fstab /etc/fstab.bak.$(date +%Y%m%d)
sudo sed -i '/^# ── root-volume offload binds/,/^\/opt\/ai_data\/system-savings\/home\/nltk_data/d' /etc/fstab
# 또는 해당 bind 14행을 '#' 주석

# 2) 현재 bind 해제 후 원본 경로로 복구
#    (데이터는 system-savings에 있으므로 root로 역이전 후 umount)
for mp in /opt/netdata /home/opc/.rustup /home/opc/.npm /home/opc/.cache \
  /home/opc/.local/share/junie /home/opc/.local/share/claude /home/opc/.local/share/uv \
  /home/opc/.local/n /home/opc/.local/bin \
  /home/opc/.local/lib/python3.9 /home/opc/.local/lib/python3.11 /home/opc/.local/lib/python3.12 \
  /home/opc/.local/lib/node_modules /home/opc/nltk_data; do
  findmnt -n "$mp" >/dev/null && sudo umount "$mp" || true
done

# 3) system-savings → 원위치 복구 (root 용량 주의; 충분할 때만)
# 예: rsync -a /opt/ai_data/system-savings/home/.cache/ /home/opc/.cache/
#     (14곳 전체 유사; netdata는 /opt/ai_data/system-savings/netdata/ → /opt/netdata/)

# 4) 일일 정리/타이머 비활성 (선택)
sudo systemctl disable --now root-volume-daily-clean.timer
sudo rm -f /etc/systemd/journald.conf.d/size.conf
sudo systemctl restart systemd-journald   # MaxRetentionSec=2week 해제

# 5) 재부팅 검증
# findmnt /home/opc/.cache  → bind 없어야 정상, 경로에 기존 데이터 존재
# systemctl is-active netdata
```

**Side Effects**:
- Rollback 시 root 용량이 즉시 증가 — offload 전 상태(≈36G/45G)로 복귀 가능
- `opencode-db-offload.service`가 아직 실행 전이면 db는 root에 남음 (무해)
- journald 상한 해제 시 로그 재팽창 가능

### 6.1 재부팅 후 체크리스트 (post-reboot)

사용자가 `sudo reboot`를 실행한 뒤 **다음 세션 시작 시** 아래를 순서대로 확인한다.

```bash
# 1) offload bind 생존 — SOURCE=system-savings 14건 + opencode 이전 후 15건
#    (TARGET는 원본 경로이므로 system-savings가 SOURCE에만 나타난다)
findmnt -n -o SOURCE | grep -c system-savings   # 기대: 14 → opencode 이전 후 15

# 2) opencode.db 이전 검증 (oneshot이 끝난 뒤)
findmnt -M /home/opc/.local/share/opencode && df -h /
ls -lh /opt/ai_data/system-savings/home/.local/share/opencode/opencode.db
#   실패 시: opencode 종료 후 sudo /usr/local/sbin/opencode-db-offload.sh

# 3) SELinux 라벨 (bind 직후 unlabeled 잔존 확인)
findmnt -n -o TARGET | while read -r t; do
  sudo ls -ldZ "$t" 2>/dev/null
done | grep unlabeled_t || echo "labels OK"

# 4) 핵심 서비스 / 경로
systemctl is-active netdata caddy.service || true
python3 -c "import site; print(site.getsitepackages())"   # python3.9 bind 경로
sudo journalctl --disk-usage   # ≤ 200M

# 5) WebObsidian 임시 drop-in 제거 (WEBOBSIDIAN-CGROUP 근본 해소)
rm -f ~/.config/systemd/user/container-webobsidian.service.d/10-slice.conf
systemctl --user daemon-reload
systemctl --user restart container-webobsidian.service
systemctl --user is-active container-webobsidian.service
```

**승인 대기 / 재검토 (재부팅 후 사용자 확인):**
- [ ] 승인 대기 3개 (wf-approvals) — 재부팅 후 재검토
- [ ] 도메인 승인 항목 — 재부팅 후 재검토
- [ ] a-1 boot LV 증량 — 용량 압박 재발 시 재고려 (현행 미증량 유지)
- [ ] a-3 fstab 미러링 — 장단점은 별도 보고, 실행 여부 미결정
- [ ] Slack Bot Token 재발급 — `auth.test` → `account_inactive` 확인됨 (C-1 테스트 완료, 키 무효)

**성공 기준:** `findmnt … system-savings` = 15, root < 25%, `opencode.db`가 `system-savings`에 있고 bind 마운트 active, `unlabeled_t` 0건.

### 6.2 데이터 이전 안전 수칙 (재발 방지, 2026-09-23)

`/home/opc/.claude/projects`(~112M, Claude Code 세션 jsonl)가 이전 중 **소실**됐다(XFS, 복구 불가) —
rsync가 dest 부모 미생성으로 실패한 뒤 `mv` + `.old` 제거가 실행됨. `history.jsonl`은 보존. 모든 홈 데이터 이전에 적용:

```bash
# 1) dry-run으로 대상/용량 먼저 확인
rsync -aX --dry-run --itemize-changes "$SRC/" "$DEST/"
# 2) dest 부모 반드시 선행 생성
mkdir -p "$DEST"
# 3) 이전 (--remove-source-files 금지 — 검증 전 소스 삭제 금지)
rsync -aX "$SRC/" "$DEST/"
# 4) 검증: 파일 수/용량 대조 후에만 원본 정리
find "$SRC" | wc -l; find "$DEST" | wc -l; du -sh "$SRC" "$DEST"
# 5) bind 마운트/용량 확인
findmnt -M "$DEST"; df -h /
```

> **금지**: rsync 실패 상태에서 `mv` + `rm`(.old)로 원본을 정리하는 것 — 검증 없는 파괴적 정리.

---

## General Rollback Checklist

Before any rollback:
1. [ ] Capture current state: `cli.py status --json > /tmp/pre-rollback-state.json`
2. [ ] Stop affected timers/services
3. [ ] Backup DB if schema involved: `podman exec postgres pg_dump -U devforge devforge_app > /tmp/backup.sql`
4. [ ] Document rollback reason in `handover.yaml` (via `update_handover.py`)
5. [ ] Test in isolated environment first (if possible)

After rollback:
1. [ ] Verify health: `cli.py status --json | jq .alerts`
2. [ ] Monitor logs for 1 hour: `journalctl --user -f`
3. [ ] Check resource usage: `cli.py status --json | jq .resources`
4. [ ] Update experiment_registry: verdict='ROLLED_BACK', rationale='<reason>'
5. [ ] Notify in Slack #devforge channel

---

## Emergency Contacts

- **Full system reset**: `systemctl --user restart devforge-*.service` (nuclear option, ~5min downtime)
- **DB corruption**: Restore from `/mnt/lv_db/backups/` (daily 02:00 UTC)
- **Stuck pods**: `podman pod rm -f data-pod; systemctl --user restart devforge-pod-*.service`

---

**Last Updated**: 2026-09-23  
**Maintainer**: Automated via rollback-guide generation (manual edits preserved)

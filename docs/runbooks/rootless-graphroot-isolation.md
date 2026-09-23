# rootless graphroot 격리 가이드 (root-storage permission denied 해결)

> Status: runbook · 작성 2026-09-23 · 대상: DevForge (Oracle Linux, podman rootless)
> 증상: rootless podman 컨테이너 기동 시 `Error: open /opt/ai_data/containers/root-storage: permission denied` (exit 126)
> 선택안: **A안** — rootless graphroot를 별도 경로로 이동해 rootful과의 **부모 공유를 제거**

---

## 0. 요약 (TL;DR)

```bash
# 전제: opc 로그인 셸 (rootless). rootful caddy는 영향 없음.
# 1) 서비스 중지  2) graphroot 이동  3) DB 경로 마이그레이션  4) 설정 변경  5) 재기동  6) 검증
systemctl --user stop devforge-watchdog.service devforge-watchdog-v2 ...   # §3
mv /opt/ai_data/containers/storage /opt/ai_data/rootless-storage           # §4
# podman DB static-dir 갱신 (§4.5, 필수) → storage.conf graphroot 수정 (§5) → daemon-reload + 재기동 (§6)
```

---

## 1. 원인 (Why)

```
/opt/ai_data/containers/
├── storage/        ← rootless graphroot (opc)     10 컨테이너 · 볼륨 6 · ~5.2G
└── root-storage/   ← rootful  graphroot (root)    caddy · 116M
```

- rootful(`/etc/containers/storage.conf`)와 rootless(`~/.config/containers/storage.conf`)가 **같은 부모 디렉터리**를 공유.
- rootless podman이 graphroot 정리/락 처리 중 형제 디렉터리 `root-storage`를 훑고, 그 안의 root 소유 `overlay/backingFsBlockDev`(mode 600)에 접근 → `permission denied`.
- 결과: 컨테이너 생성 실패(`exit 126`), 특히 재시작이 잦은 서비스에서 간헐 재발.

**핵심**: `root-storage` 삭제는 **불가**(rootful caddy가 사용 중, 삭제 시 caddy 이미지/컨테이너 소실). 해결은 **경로 분리**.

---

## 2. 사전 확인 (Pre-flight)

```bash
# (a) 현재 graphroot 확인 — 두 값이 서로 다른 부모여야 목표 달성
podman info --format 'rootless={{.Store.GraphRoot}}'        # → /opt/ai_data/containers/storage
sudo podman info --format 'rootful={{.Store.GraphRoot}}'    # → /opt/ai_data/containers/root-storage

# (b) rootful이 실제 사용 중인지 (caddy 존재 확인 — 삭제 금지 근거)
sudo podman ps -a --format '{{.Names}} {{.Status}}'

# (c) 여유 공간 (이동은 같은 파일시스템 내 rename → 순간 추가 공간 불요)
df -h /opt/ai_data

# (d) 실행 중 rootless 컨테이너/파드/볼륨 인벤토리 (복구 검증 기준)
podman ps --format '{{.Names}}' | sort > /tmp/pre_containers.txt
podman pod ps --format '{{.Name}}' | sort > /tmp/pre_pods.txt
podman volume ls --format '{{.Name}}' | sort > /tmp/pre_volumes.txt
podman images --format '{{.Repository}}:{{.Tag}}' | sort > /tmp/pre_images.txt
wc -l /tmp/pre_*.txt
```

---

## 3. 서비스 중지 (rootless 전부)

Quadlet 유닛과 pod를 정지한다. **rootful caddy는 건드리지 않는다.**

```bash
# 3-1. Quadlet 기반 유닛 정지 (pod 단위 유닛 포함)
systemctl --user stop \
  devforge-watchdog.service devforge-watchdog-v2.service \
  container-devforge-fastapi.service \
  container-devforge-mcp.service \
  container-devforge-worker.service \
  container-flaresolverr.service \
  container-postgres.service \
  container-webobsidian.service \
  data-pod.service svc-pod.service 2>&1

# 3-2. Quadlet에 없는 임시 컨테이너 정리 (있으면)
for c in devforge-inference modest_chebyshev; do
  podman stop "$c" 2>/dev/null || true
done

# 3-3. 잔여 확인 — 아래가 비어야 함
podman ps --format '{{.Names}}'
```
> ⚠️ `podman ps`에 컨테이너가 남아 있으면 `podman stop $(podman ps -q)`로 마저 정지.

---

## 4. graphroot 이동

같은 파일시스템 내 **rename**(즉시, 공간 추가 불요).

```bash
mv /opt/ai_data/containers/storage /opt/ai_data/rootless-storage
ls -ld /opt/ai_data/rootless-storage            # 이동 확인
ls /opt/ai_data/containers/                      # storage 없음, root-storage만 남아야 함
```

---

### 4.5 podman DB static-dir 마이그레이션 (podman ≥5, **필수**)

graphroot를 옮기면 podman이 `database static dir ... does not match our static dir ...`로 **모든 명령을 거부**한다.
libpod DB(`graphroot/db.sql`, sqlite)의 `DBConfig`·`VolumeConfig`에 이전 경로가 기록돼 있기 때문. DB를 갱신한다.

```bash
cp -a /opt/ai_data/rootless-storage/db.sql /opt/ai_data/rootless-storage/db.sql.bak.$(date +%Y%m%d)
python3 - <<'PY'
import sqlite3
db = '/opt/ai_data/rootless-storage/db.sql'
OLD, NEW = '/opt/ai_data/containers/storage', '/opt/ai_data/rootless-storage'
c = sqlite3.connect(db)
c.execute('UPDATE DBConfig SET StaticDir=replace(StaticDir,?,?), '
          'GraphRoot=replace(GraphRoot,?,?), VolumeDir=replace(VolumeDir,?,?)',
          (OLD, NEW, OLD, NEW, OLD, NEW))
for name, js in list(c.execute('SELECT Name, JSON FROM VolumeConfig')):
    if js and OLD.encode() in js:
        c.execute('UPDATE VolumeConfig SET JSON=? WHERE Name=?', (js.replace(OLD.encode(), NEW.encode()), name))
c.commit()
PY
podman ps   # 이제 동작해야 함 (rc=0)
```
> podman 5.6.0(sqlite) 기준 실측. 경로가 다른 테이블에 더 있으면 `PRAGMA table_info` + `LIKE '%<old>%'`로 전수 확인.

## 5. 설정 변경

rootless graphroot를 새 경로로 지정한다.

```bash
cp ~/.config/containers/storage.conf ~/.config/containers/storage.conf.bak.$(date +%Y%m%d)
sed -i 's#^graphroot = "/opt/ai_data/containers/storage"#graphroot = "/opt/ai_data/rootless-storage"#' \
  ~/.config/containers/storage.conf
cat ~/.config/containers/storage.conf        # graphroot가 새 경로인지 확인
```

> `runroot`는 기존 값(`/run/user/1000/containers`) 유지. rootful `/etc/containers/storage.conf`는 **수정하지 않는다**.

---

## 6. 재기동

```bash
systemctl --user daemon-reload

systemctl --user start \
  svc-pod.service data-pod.service \
  container-postgres.service container-devforge-fastapi.service \
  container-devforge-mcp.service container-devforge-worker.service \
  container-flaresolverr.service container-webobsidian.service 2>&1

# 정상 기동 후 watchdog 재개 (legacy + shadow v2)
systemctl --user start devforge-watchdog.service devforge-watchdog-v2.service
```

---

## 7. 검증

```bash
# (a) graphroot 격리 확인 — 부모가 서로 달라야 성공
podman info --format 'rootless={{.Store.GraphRoot}}'        # → /opt/ai_data/rootless-storage
sudo podman info --format 'rootful={{.Store.GraphRoot}}'    # → /opt/ai_data/containers/root-storage

# (b) 컨테이너/파드/볼륨/이미지 복구 대조 (pre와 diff가 비어야 정상)
podman ps --format '{{.Names}}' | sort > /tmp/post_containers.txt
podman pod ps --format '{{.Name}}' | sort > /tmp/post_pods.txt
podman volume ls --format '{{.Name}}' | sort > /tmp/post_volumes.txt
podman images --format '{{.Repository}}:{{.Tag}}' | sort > /tmp/post_images.txt
diff /tmp/pre_containers.txt /tmp/post_containers.txt
diff /tmp/pre_pods.txt /tmp/post_pods.txt
diff /tmp/pre_volumes.txt /tmp/post_volumes.txt
diff /tmp/pre_images.txt /tmp/post_images.txt

# (c) 서비스 상태 + 헬스
systemctl --user is-active container-postgres.service container-devforge-fastapi.service \
  svc-pod.service data-pod.service devforge-watchdog-v2.service
curl -s -m 5 http://127.0.0.1:8089/health        # ebook-api (경로 문제와 별개, 정상 확인)
podman exec postgres psql -U devforge -d devforge_app -c 'select 1'

# (d) 재발 감시 — v2 로그에 permission denied 없어야 함
journalctl --user -u devforge-watchdog-v2 --since "-5 min" | grep -i "permission denied" || echo "OK: no permission denied"
```

---

## 8. 롤백

문제 시 원복(서비스 중지 → 경로/설정 복원 → 재기동).

```bash
systemctl --user stop devforge-watchdog.service devforge-watchdog-v2.service svc-pod.service data-pod.service \
  container-postgres.service container-devforge-fastapi.service \
  container-devforge-mcp.service container-devforge-worker.service container-flaresolverr.service
cp -a /opt/ai_data/rootless-storage/db.sql.bak.<날짜> /opt/ai_data/rootless-storage/db.sql   # DB 경로 원복
mv /opt/ai_data/rootless-storage /opt/ai_data/containers/storage
cp ~/.config/containers/storage.conf.bak.<날짜> ~/.config/containers/storage.conf
systemctl --user daemon-reload
systemctl --user start svc-pod.service data-pod.service container-postgres.service \
  container-devforge-fastapi.service container-devforge-mcp.service \
  container-devforge-worker.service container-flaresolverr.service container-webobsidian.service
systemctl --user start devforge-watchdog.service devforge-watchdog-v2.service
```

---

## 9. 주의사항

- **SELinux**: 동일 볼륨 내 rename이므로 라벨 재부여 불필요. 경로 변경 후 문제가 있으면 `sudo restorecon -R /opt/ai_data/rootless-storage`.
- **Quadlet 경로 하드코딩**: 유닛이 graphroot를 명시하면 수정 필요. 현재 Quadlet은 graphroot를 참조하지 않음(기본값 사용) → `daemon-reload`만으로 반영.
- **podman DB static-dir**: graphroot 이동 시 **§4.5를 반드시 선행**하지 않으면 podman이 `database configuration mismatch`로 전 명령을 거부한다(podman ≥5 sqlite). DB 백업(`db.sql.bak`)은 롤백에 필요.
- **legacy watchdog 간섭**: `devforge-watchdog.service`(legacy)가 정지된 서비스를 자동 재기동하므로 §3에서 함께 정지한다.
- **동시 작업 금지**: rootless podman 전체 정지가 필요하므로 **다른 rootless 작업이 끝난 뒤** 실행.
- **watchdog shadow-run**: 본 작업으로 중단되므로 완료 후 재기동. 24h 모니터링 타이머는 재기동 시점부터 재계산 권장.
- **성공 기준**: §7(a)의 두 graphroot 부모가 다르고, §7(b) diff가 비어 있으며, §7(d)에 `permission denied`가 없을 것.

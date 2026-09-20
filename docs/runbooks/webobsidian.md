# Runbook — WebObsidian (vault 웹 열람/편집)

> 목적: 브라우저에서 Obsidian vault를 열람/편집하고, 특정 노트를 읽기 전용으로 외부 공유한다.
> 관련: `~/Obsidian/IMPLEMENTATION_GUIDE.md` §7.5 · KV `WEBOBSIDIAN-PASSWORD`
> **비밀번호 값은 어떤 문서에도 기재하지 않는다.**

## 접근 모델 (2026-09-20)

| 대상 | URL | 인증 | 용도 |
|------|-----|------|------|
| 루트 앱 | `https://notes.152-69-229-246.nip.io/` | 비밀번호 | 사용자 편집(읽기/쓰기), 외부 공유 안 함 |
| 공유 링크 | `https://notes.152-69-229-246.nip.io/share/<id>` | 없음(읽기 전용) | 외부 제공용(노트 단위) |

## 구성

- 컨테이너: `container-webobsidian.service` (rootless podman), `127.0.0.1:8787`.
- 바인드: `~/Obsidian` → `/vault` (rw), 볼륨 `webobsidian-data` → `/data`.
- Caddy: **`notes.152-69-229-246.nip.io` 사이트 블록**(루트 → `127.0.0.1:8787`).
  기존 `handle_path /notes/*`·`/obsidian/*` 라우트는 **제거**(SPA base=`/` 불일치).
- 인증: KV `WEBOBSIDIAN-PASSWORD` (EnvironmentFile 주입). 값은 문서/로그에 남기지 않는다.

## 공유 링크 생성

세션 인증 후 `POST /api/shares {path}` → `{share:{id}}`. 공개 열람은 `GET /share/<id>`(SSR, 읽기 전용).

```bash
# 사용자에게 줄 링크 발급(읽기전용 + 편집 2줄)
~/Obsidian/scripts/webobsidian-url.py --path 30_Responses/x.md
```

## 비밀번호 변경 (값 미기재)

1. KV `WEBOBSIDIAN-PASSWORD` 갱신(`kv-common-prod-krc`, 쓰기 SP).
2. 컨테이너 재시작 → `ExecStartPre`가 env 재생성.
3. 앱은 저장 비밀번호 **최소 6자**를 강제하므로, 4자리 등은 KV 오버라이드로만 유효하다.
   저장 해시를 직접 지정하려면 `/data/settings.json`의 `auth.userPasswordHash`에 scrypt 해시를 넣는다
   (`scrypt$<saltHex>$<hashHex>`; 해시 계산은 컨테이너 내 `hashPassword()` 사용).

## 검증

```bash
curl -s https://notes.152-69-229-246.nip.io/healthz                       # {"ok":true}
curl -s -o /dev/null -w '%{http_code}\n' https://notes.152-69-229-246.nip.io/api/files   # 401(미인증)
curl -s -o /dev/null -w '%{http_code}\n' https://notes.152-69-229-246.nip.io/share/<id>  # 200(공유)
```

## 알려진 이슈

- 컨테이너가 **stale child cgroup EBUSY**로 재시작 실패할 수 있다(근본 해결: 리부팅 후 drop-in 제거).
  임시 우회: drop-in `Slice=`를 새 이름으로 전환 후 `daemon-reload`.
  파일: `~/.config/systemd/user/container-webobsidian.service.d/10-slice.conf`.
- 비밀번호가 컨테이너 env로 주입되어 `podman inspect`에 평문이 보일 수 있다(교체 권장).

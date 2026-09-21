# User systemd unit sources (version-controlled mirror)

이 디렉터리는 **hand-written user unit의 버전관리 사본**입니다.
실제 배포 위치는 `~/.config/systemd/user/` 입니다.

## 범위

- 포함: 손으로 작성한 `.service` / `.timer` (devforge-*, cashbook, ebook-*,
  webobsidian, watchdog, kv-*, workspace-*, 참조/프록시 등).
- 제외: Quadlet이 런타임에 생성하는 `container-*.service`
  (`/run/user/1000/systemd/generator/` 산출물, 소스 아님).

## 배포 방법

```bash
cp systemd/user/*.service systemd/user/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
# 필요 시: systemctl --user enable --now <unit>
```

## 주의

- 유닛에 **시크릿을 인라인하지 마세요.** 환경이 필요하면 `EnvironmentFile=`에
  KV 산출 파일을 지정하거나 `kv-fetch-env.py` 래퍼를 사용합니다.
- 실제 파일과 이 사본이 다르면 **실제 파일이 우선**입니다(Code is SSOT).
  차이를 발견하면 이 사본을 갱신하세요.

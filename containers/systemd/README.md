# Quadlet unit sources (version-controlled mirror)

이 디렉터리는 **Quadlet 소스 파일의 버전관리 사본**입니다.
실제 배포 위치는 `~/.config/containers/systemd/` 이며, systemd가 여기서
`.container`/`.pod`를 읽어 `container-*.service`를 런타임에 생성합니다.

## 왜 사본을 두는가

- 기존에는 유닛 정의가 `~/.config/`에만 있어 **git 이력이 없었고**, 변경이
  추적되지 않았습니다 (예: postgres/cashbook 유닛은 커밋되지 않음).
- 이 사본이 배포 정의의 참조점입니다. 실제 파일과 다르면 실제 파일이 우선입니다
  (Code is SSOT 원칙). 차이를 발견하면 이 사본을 갱신하세요.

## 배포 방법

```bash
# 이 디렉터리의 파일을 실제 경로로 복사(또는 심볼릭 링크)한 뒤 재생성
cp containers/systemd/*.container ~/.config/containers/systemd/
cp containers/systemd/*.pod       ~/.config/containers/systemd/
systemctl --user daemon-reload
```

## 주의

- `.bak`, `.container.disabled`, `_disabled/`, `archive/` 는 **커밋하지 않습니다**
  (활성 소스만 관리). 비활성화는 `.container.disabled`로 이름을 바꿔 보존합니다.
- 유닛에 **시크릿을 인라인하지 마세요.** KV는 `kv-fetch-env.py` /
  `kv-export-env.sh` / `kv-to-credential.sh`로 주입합니다.
- Quadlet이 생성하는 `~/.config/systemd/user/container-*.service`는 **추적 대상이
  아닙니다**(런타임 generator 산출물).

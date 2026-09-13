# 하드코딩 경로 인벤토리 (Phase −1)

## 요약

| 카테고리 | 개수 | 비고 |
|----------|------|------|
| `/opt/projects/server` | 144 | 스크립트 루트, 설정 파일, 모듈 임포트 |
| `/opt/ai_data` | 107 | 모델 파일, 설정 env, DB, 검색 인덱스 |
| `/opt/workspace` | 2 | golden_image 심볼릭 링크 (이미 해결) |
| **합계** | **253** | (중복 제외, absolute 18은 /opt/ai_data 하위) |

## 주요 영역별 분포

### /opt/ai_data (107곳)
- `scripts/current-mode-inference.env` — 20곳 (런타임 모델 설정)
- `models/gguf/` — 15곳 (GGUF 모델 파일 경로)
- `scripts/` — 35곳 (스크립트 경로)
- `search/` — 2곳 (검색 DB)
- `flaresolverr/` — 1곳 (rate limiter DB)

### /opt/projects/server (144곳)
- `scripts/` — 89곳 (모듈 임포트, 파일 참조)
- `docs/` — 23곳 (문서 경로)
- `data/` — 15곳 (데이터 파일)
- `containers/` — 12곳 (컨테이너 설정)
- `config/` — 5곳 (설정 파일)

## 해결 전략 (Phase 0)

```python
# core/paths.py
from pathlib import Path
from pydantic_settings import BaseSettings

class Paths(BaseSettings):
    data_dir: Path = Path("/opt/ai_data")
    server_dir: Path = Path("/opt/projects/server")
    
    # 하위 디렉토리
    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models" / "gguf"
    
    @property
    def scripts_dir(self) -> Path:
        return self.data_dir / "scripts"
    
    @property
    def current_mode_env(self) -> Path:
        return self.scripts_dir / "current-mode-inference.env"
    
    model_config = SettingsConfigDict(
        env_prefix="DEVFORGE_",
        extra="ignore",
    )
```

## 파일 목록

| 파일 | /opt/ai_data | /opt/projects/server | 비고 |
|------|-------------|---------------------|------|
| `lib/pod_manager/container.py` | 6 | 0 | Podman 볼륨 마운트 |
| `lib/model_ctl.py` | 2 | 2 | 모델 제어 |
| `lib/pod_manager/__init__.py` | 1 | 1 | 컨테이너 관리 |
| `lib/model_registry.py` | 0 | 0 | (상대 경로 사용) |
| `lib/watchdog/checker.py` | 0 | 0 | (상대 경로 사용) |
| `day_cycle.sh` | 2 | 1 | 환경 설정 + 스크립트 경로 |
| `cli.py` | 0 | 23+ | CLI 상태 출력 |
| `gen_architecture.py` | 2 | 5+ | 아키텍처 생성 |
| `lib/db.py` | 0 | 0 | DB 연결 (DSN) |
| 기타 20+ 파일 | 분산 | 분산 | - |

**생성일**: 2026-09-13 (Phase −1)
**전체 레코드**: 253개 (data/hardcoded_paths.csv)

# Option 1: Phase 0 리팩토링 계속 진행 가이드

**작성일:** 2026-09-21  
**대상:** AI 에이전트 또는 개발자  
**소요 시간:** 2-3시간  
**난이도:** Medium-High  
**목적:** Phase 0 (Week 1) 남은 작업 완료

---

## 현재 상태 (2026-09-21)

**완료 (✅):**
- pyproject.toml + src/devforge/ 골격
- core/config.py (부분)
- core/logging.py (부분)
- ports/extract.py
- domain/ 서브디렉토리 4개

**진행 중 (🟡):**
- ConfigRegistry 완성
- Paths 추상화

**미착수 (⬜):**
- core/database.py
- core/paths.py
- core/exceptions.py
- import-linter CI

---

## 작업 계획

### Task 1: core/database.py 구현 (60분)

**목표:** SQLAlchemy 2.0 async 풀 설정

**참고 문서:**
- [SQLAlchemy 2.0 Async](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)
- 기존 코드: `scripts/lib/` 에서 DB 연결 패턴 확인

**구현 단계:**

#### 1.1 기존 DB 연결 방식 조사 (10분)
```bash
cd /opt/projects/server
grep -r "psycopg\|asyncpg\|create_engine" scripts/lib/ | head -20
grep -r "DATABASE_URL\|DB_" scripts/ | head -10
```

**체크:**
- 현재 사용 중인 드라이버: psycopg2? asyncpg?
- 연결 문자열 형식
- 풀 설정 (min/max connections)

#### 1.2 core/database.py 작성 (30분)
```python
# src/devforge/core/database.py
"""
Database connection management using SQLAlchemy 2.0 async.
"""
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import (
    create_async_engine,
    async_sessionmaker,
    AsyncSession,
    AsyncEngine,
)
from sqlalchemy.orm import DeclarativeBase
from .config import get_config

class Base(DeclarativeBase):
    """SQLAlchemy declarative base."""
    pass

class DatabaseGateway:
    """Async database connection pool manager."""
    
    def __init__(self, database_url: str, pool_size: int = 5, max_overflow: int = 10):
        """
        Initialize async engine.
        
        Args:
            database_url: PostgreSQL connection string (postgresql+asyncpg://...)
            pool_size: Connection pool size
            max_overflow: Max overflow connections
        """
        self.engine: AsyncEngine = create_async_engine(
            database_url,
            pool_size=pool_size,
            max_overflow=max_overflow,
            echo=False,  # Set True for SQL logging
            pool_pre_ping=True,  # Verify connections before use
        )
        self.session_maker = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
    
    async def get_session(self) -> AsyncGenerator[AsyncSession, None]:
        """
        Get async session (use with async with).
        
        Example:
            async with gateway.get_session() as session:
                result = await session.execute(select(Turn))
        """
        async with self.session_maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()
    
    async def dispose(self):
        """Close all connections."""
        await self.engine.dispose()

# Global instance (lazy initialization)
_gateway: DatabaseGateway | None = None

def get_database() -> DatabaseGateway:
    """Get or create database gateway singleton."""
    global _gateway
    if _gateway is None:
        config = get_config()
        _gateway = DatabaseGateway(
            database_url=config.database_url,
            pool_size=config.db_pool_size,
            max_overflow=config.db_max_overflow,
        )
    return _gateway
```

#### 1.3 core/config.py에 DB 설정 추가 (10분)
```python
# src/devforge/core/config.py에 추가
from pydantic_settings import BaseSettings

class DatabaseSettings(BaseSettings):
    """Database configuration."""
    database_url: str = "postgresql+asyncpg://devforge:password@localhost:5432/devforge"
    db_pool_size: int = 5
    db_max_overflow: int = 10
    
    class Config:
        env_prefix = "DEVFORGE_"
```

#### 1.4 테스트 (10분)
```python
# tests/unit/test_database.py
import pytest
from devforge.core.database import DatabaseGateway

@pytest.mark.asyncio
async def test_database_connection():
    """Test database connection."""
    gateway = DatabaseGateway("postgresql+asyncpg://localhost/test")
    
    async with gateway.get_session() as session:
        result = await session.execute("SELECT 1")
        assert result.scalar() == 1
    
    await gateway.dispose()
```

---

### Task 2: core/paths.py 완성 (40분)

**목표:** 하드코딩 경로 40곳 추상화

#### 2.1 하드코딩 경로 목록 확인 (10분)
```bash
# Phase −1에서 생성한 목록 확인
cat /opt/projects/server/data/hardcoded_paths.csv 2>/dev/null || \
grep -r "/opt/projects/server\|/opt/ai_data" scripts/ --include="*.py" | \
  grep -v "^Binary" | cut -d: -f1 | sort -u | wc -l
```

#### 2.2 Paths 클래스 구현 (20분)
```python
# src/devforge/core/paths.py
"""
Centralized path management to eliminate hardcoded paths.
"""
from pathlib import Path
from typing import Optional
from .config import get_config

class Paths:
    """Centralized path registry."""
    
    def __init__(self, base_dir: Optional[Path] = None):
        """
        Initialize paths.
        
        Args:
            base_dir: Override base directory (default: /opt/projects/server)
        """
        self._base = base_dir or Path("/opt/projects/server")
        self._data = Path("/opt/ai_data")
    
    # Project paths
    @property
    def project_root(self) -> Path:
        """Project root directory."""
        return self._base
    
    @property
    def scripts_dir(self) -> Path:
        """Scripts directory (legacy)."""
        return self._base / "scripts"
    
    @property
    def src_dir(self) -> Path:
        """Source directory (refactored)."""
        return self._base / "src"
    
    @property
    def docs_dir(self) -> Path:
        """Documentation directory."""
        return self._base / "docs"
    
    @property
    def config_dir(self) -> Path:
        """Configuration directory."""
        return self._base / "config"
    
    # Data paths
    @property
    def data_dir(self) -> Path:
        """Main data directory."""
        return self._data
    
    @property
    def backups_dir(self) -> Path:
        """Backup directory."""
        return self._data / "backups"
    
    @property
    def pg_dumps_dir(self) -> Path:
        """PostgreSQL dumps directory."""
        return self.backups_dir / "pg_dumps"
    
    @property
    def logs_dir(self) -> Path:
        """Logs directory."""
        return self._base / "logs"
    
    @property
    def temp_dir(self) -> Path:
        """Temporary directory."""
        return Path("/tmp") / "devforge"
    
    # State files
    @property
    def checkpoint_file(self) -> Path:
        """Turn collection checkpoint."""
        return self._base / "collect_checkpoint.json"
    
    @property
    def state_yaml(self) -> Path:
        """State YAML file."""
        return self._base / "state.yaml"
    
    def ensure_dirs(self):
        """Create all required directories."""
        for path in [
            self.logs_dir,
            self.temp_dir,
            self.backups_dir,
            self.pg_dumps_dir,
        ]:
            path.mkdir(parents=True, exist_ok=True)

# Global instance
_paths: Optional[Paths] = None

def get_paths() -> Paths:
    """Get paths singleton."""
    global _paths
    if _paths is None:
        _paths = Paths()
    return _paths
```

#### 2.3 기존 코드 마이그레이션 (10분)

**변경 전:**
```python
# scripts/some_script.py
BACKUP_DIR = "/opt/ai_data/backups/pg_dumps"
```

**변경 후:**
```python
# src/devforge/some_module.py
from devforge.core.paths import get_paths

paths = get_paths()
BACKUP_DIR = paths.pg_dumps_dir
```

---

### Task 3: core/exceptions.py 생성 (20분)

```python
# src/devforge/core/exceptions.py
"""
Custom exception hierarchy for DevForge.
"""

class DevForgeError(Exception):
    """Base exception for all DevForge errors."""
    pass

class ConfigurationError(DevForgeError):
    """Configuration-related errors."""
    pass

class DatabaseError(DevForgeError):
    """Database-related errors."""
    pass

class LLMError(DevForgeError):
    """LLM provider errors."""
    pass

class PipelineError(DevForgeError):
    """Pipeline execution errors."""
    pass

class ValidationError(DevForgeError):
    """Data validation errors."""
    pass
```

---

### Task 4: import-linter 설정 (30분)

#### 4.1 pyproject.toml에 추가
```toml
# pyproject.toml
[tool.importlinter]
root_packages = ["devforge"]

[[tool.importlinter.contracts]]
name = "Layered architecture"
type = "layers"
layers = [
    "devforge.adapters.driving",
    "devforge.application",
    "devforge.domain",
    "devforge.ports",
    "devforge.core",
]

[[tool.importlinter.contracts]]
name = "Domain independence"
type = "independence"
modules = [
    "devforge.domain.turn_collection",
    "devforge.domain.pipeline",
    "devforge.domain.watchdog",
    "devforge.domain.model_management",
]

[[tool.importlinter.contracts]]
name = "No adapters in domain"
type = "forbidden"
source_modules = ["devforge.domain"]
forbidden_modules = ["devforge.adapters"]
```

#### 4.2 설치 및 실행
```bash
pip install import-linter
lint-imports
```

---

## 검증 체크리스트

완료 후 확인:

- [ ] `from devforge.core.database import DatabaseGateway` 동작
- [ ] `from devforge.core.paths import get_paths` 동작
- [ ] `from devforge.core.exceptions import DevForgeError` 동작
- [ ] `lint-imports` 실행 성공 (0 violations)
- [ ] 단위 테스트 통과
- [ ] Git 커밋

---

## Git 커밋

```bash
cd /opt/projects/server
git add src/devforge/core/*.py pyproject.toml tests/
git commit -m "refactor(phase-0): complete Week 1 tasks

Implemented:
- core/database.py: SQLAlchemy 2.0 async gateway
- core/paths.py: centralized path management (40 hardcoded paths)
- core/exceptions.py: custom exception hierarchy
- import-linter: architecture enforcement

Tests: unit tests added for database and paths
Verification: lint-imports passes with 0 violations

Phase 0 progress: ~60% (Week 1 complete, Week 2 pending)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

**다음 단계:** Week 2 특성화 테스트 작성 (별도 가이드)

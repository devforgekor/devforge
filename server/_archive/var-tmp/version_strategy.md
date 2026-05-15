# 📡 DevForge 버전 관리 및 안정성 전략

## 배경
DevForge는 Flask → FastAPI, PostgreSQL, pgvector, MCP 서버, Flutter/React Native, OpenAI API 등 **수십 개의 외부 레퍼런스**로 구성됩니다. 
이들의 버전을 체계적으로 추적하지 않으면 예측 불가능한 장애가 발생합니다.

---

## 1️⃣ 안정 버전 기준 정의

### SemVer 기준
- ✅ `1.0.0` 이상: 안정 버전 (사용 권장)
- ⚠️  `0.x.x`: 불안정 (신중하게 검토 후 사용)

### GitHub 기준
- ✅ `Latest` 또는 `Stable` 태그: 권장
- ❌ `Pre-release` (베타/RC): 제외

### 변경 로그 기준
- ⚠️  "Breaking Changes" 있음: 신중한 검토 필수
- ✅ Minor/Patch만 있음: 자동 업데이트 가능

---

## 2️⃣ 자동 추적 시스템 구축

### GitHub 기반
- **저장소 Watch**: 각 저장소 페이지 → Watch → Releases
- **Release Monitor 도입** (자체 호스팅):
  ```bash
  # docker-compose.yml에 추가
  github-release-monitor:
    image: iamspido/github-release-monitor
    environment:
      - TELEGRAM_TOKEN=${TG_TOKEN}
      - CHECK_INTERVAL=3600
    # 스크린에 릴리스 알림 표시
  ```

### 패키지 레지스트리 모니터링
- npm: `npm view <package> versions` (자동 스크립트)
- PyPI: `pip index versions <package>` (자동 스크립트)
- Docker Hub: API 폴링 (매주 확인)

---

## 3️⃣ 구성 요소별 버전 관리 전략

### 모바일 (Flutter/React Native)
```yaml
# pubspec.yaml (DO NOT USE ^version)
dependencies:
  flutter_inappwebview: 6.0.0     # ✅ 정확 버전
  # ❌ flutter_inappwebview: ^6.0.0  (범위 지정 금지)

# 필수: pubspec.lock 저장소 커밋
```

### FastAPI 서버
```
# requirements.txt (정확 버전 필수)
fastapi==0.100.0
sqlalchemy==2.0.21
pydantic==2.3.0
# ...

# 또는 poetry 사용
# poetry.lock 필수 커밋
```

### MCP 서버
```json
{
  "mcpServers": {
    "memory-server": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem@0.5.1"]
      // ✅ 정확 버전 명시
      // ❌ @latest 사용 금지
    }
  }
}
```

### PostgreSQL + pgvector
```dockerfile
# Dockerfile
FROM postgres:15          # ✅ 메이저 버전 명시
# ❌ FROM postgres:latest

RUN apt-get install -y postgresql-15-pgvector=0.4.4-1.pgdg120+1
# ✅ 정확 버전 명시
```

### OpenAI API
```python
# 모델명 정확히 명시
embedding_model = "text-embedding-3-small"
llm_model = "gpt-4-0613"  # 스냅샷 버전 사용

# ❌ "gpt-4o" (최신, 예측 불가)
```

---

## 4️⃣ Dependabot 설정 (안정성 중심)

```yaml
# .github/dependabot.yml
version: 2
updates:
  # Python
  - package-ecosystem: "pip"
    directory: "/"
    schedule:
      interval: "monthly"  # 월 1회만 체크
    versioning-strategy: "increase"
    allow:
      - dependency-type: "direct"
    ignore:
      - dependency-name: "*"
        update-types: ["version-update:semver-major"]  # ✅ Major 제외
    open-pull-requests-limit: 3

  # npm (모바일)
  - package-ecosystem: "npm"
    directory: "/mobile"
    schedule:
      interval: "monthly"
    versioning-strategy: "increase"
    allow:
      - dependency-type: "direct"
    ignore:
      - dependency-name: "*"
        update-types: ["version-update:semver-major"]
    open-pull-requests-limit: 3

  # Docker
  - package-ecosystem: "docker"
    directory: "/"
    schedule:
      interval: "monthly"
    ignore:
      - dependency-name: "*"
        update-types: ["version-update:semver-major"]
```

---

## 5️⃣ 업데이트 검증 워크플로우

Dependabot PR 받음 → 다음 체크리스트 실행:

- [ ] **변경 로그 읽기**: Breaking Changes 확인
- [ ] **호환성 섹션**: 지원 플랫폼/하위 버전 변경 확인
- [ ] **로컬 테스트**: 기존 기능 정상 동작 확인
- [ ] **스테이징 배포**: 테스트 서버에 배포
- [ ] **통합 테스트**: 모든 기능 검증
- [ ] **메인 병합**: 모든 테스트 통과 후

---

## 6️⃣ 관리 프로세스

### 주간 (매주 금요일)
```bash
# 1. Dependabot PR 확인
# 2. 변경 로그 스캔
# 3. 테스트 서버 배포 (월 1회)
```

### 월간 (매월 첫째 주)
```bash
# 1. 누적된 Dependabot PR 일괄 검토
# 2. 보안 업데이트 우선 처리
# 3. Major 업데이트 수동 검토
# 4. 메인 브랜치 병합
```

### 분기별 (매 분기)
```bash
# 1. 종속성 감사
# 2. 미사용 라이브러리 제거
# 3. 보안 취약점 스캔
# 4. Major 버전 업그레이드 계획
```

---

## 7️⃣ 폐쇄망 환경 (회사 서버)

### Nexus Repository 도입
```yaml
# docker-compose.yml
nexus:
  image: sonatype/nexus3:latest
  volumes:
    - nexus-data:/nexus-data
  ports:
    - 8081:8081
```

- npm, PyPI, Docker Hub 프록시
- 내부에서 사용하는 버전 통제
- 화이트리스트 기반 승인

### CI/CD 강제 정책
```bash
# .github/workflows/check-versions.yml
- name: Enforce Exact Versions
  run: |
    # requirements.txt 체크 (정확 버전만 허용)
    ! grep -E '^[a-z].*[^=]=[^=]|>|<|~|\^' requirements.txt
    # package-lock.json 필수
    test -f mobile/package-lock.json
```

---

## 📊 정리표

| 항목 | DevForge | 설명 |
|------|---------|------|
| 버전 정책 | 정확 버전 (==, ==, 등) | 범위 지정 금지 |
| Lock 파일 | 필수 커밋 | poetry.lock, package-lock.json 등 |
| Dependabot | 월 1회 체크 | Major는 수동 검토 |
| 검증 | 스테이징 배포 | 프로덕션 전 반드시 테스트 |
| 폐쇄망 | Nexus 프록시 | 버전 완벽 통제 |

---

## 🚀 즉시 액션

1. ✅ GitHub Dependabot 설정 (dependabot.yml 작성)
2. ✅ 현재 모든 의존성 정확 버전으로 고정
3. ✅ lock 파일 커밋
4. ✅ CI/CD에 버전 검증 추가
5. 🆕 Release Monitor 구축 (선택)
6. 🆕 Nexus (폐쇄망용, 나중에)


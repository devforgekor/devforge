# Deep Dive: etextbook-2026 Docker 배포 계획

> 2026-08-28 — NW.js 기반 전자책 뷰어를 OCI Ampere A1 (ARM64)에서 Docker로 웹 서빙하기 위한 아키텍처 설계
> 검증 완료: nginx:alpine ARM64 호환성, Angular SPA 라우팅, EPUB3 MIME 타입

## 1. 프로젝트 개요

### 1.1 목적

Azure Blob Storage에 저장된 NW.js 기반 전자책 뷰어(`etextbook-2026`)를 OCI mesids 계정의 Ampere A1 인스턴스에서 Docker 컨테이너로 웹 서빙하여, exe 런처 없이 브라우저로 접근 가능하게 한다.

### 1.2 소스 위치

| 항목 | 값 |
|------|-----|
| Azure Blob Account | `stshareddevforgeprodkrc` |
| Container | `etextbook-2026` |
| 브라우블 수 | 7,399개 |
| 총 크기 | ~6.99 GB |
| 대상 경로 | `middle/pe/app/` |

### 1.3 목표 아키텍처

```
┌─────────────────────────────────────────────┐
│  OCI Ampere A1 (ARM64, A1.Flex)             │
│  ┌───────────────────────────────────────┐  │
│  │  Docker Container                     │  │
│  │  ┌─────────────────────────────────┐  │  │
│  │  │  nginx:alpine (linux/arm64)     │  │  │
│  │  │                                 │  │  │
│  │  │  /usr/share/nginx/html/         │  │  │
│  │  │  ├── viewer/contents/  (SPA)    │  │  │
│  │  │  ├── viewer/ebook/     (SPA)    │  │  │
│  │  │  └── resource/contents/ (EPUB3) │  │  │
│  │  │                                 │  │  │
│  │  │  Port: 80 → host:80            │  │  │
│  │  └─────────────────────────────────┘  │  │
│  └───────────────────────────────────────┘  │
└─────────────────────────────────────────────┘
```

## 2. 파일 분석

### 2.1 NW.js vs Web 호환성 분석

| 경로 | 설명 | NW.js 의존성 | Web 호환 |
|------|------|-------------|---------|
| `viewer/contents/index.html` | Angular SPA 메인 | 없음 | ✅ |
| `viewer/ebook/index.html` | Angular SPA 대체 | 없음 | ✅ |
| `viewer/contents/main.*.js` | Angular 번들 | `nw.setImmediate` (2회, 폴백 존재) | ✅ |
| `resource/contents/` | EPUB3 콘텐츠 | 없음 | ✅ |
| `viewer/window.html` | NW.js 컨테이너 | `require('url')` | ❌ |
| `viewer/blank.html` | NW.js 에러 페이지 | `require('url')` | ❌ |
| `js/inject_start.min.js` | NW.js inject | `jj._path` | ❌ |
| `js/inject_end.min.js` | NW.js inject | `window.nw`, `require()` | ❌ |
| `launcher.exe` | Windows 런처 | N/A | ❌ |

**핵심 발견**: Angular 뷰어 앱은 `main.js`에서 `nw.setmediate`를 2회 사용하지만, Zone.js에 브라우저 폴백이 존재하여 웹 환경에서 정상 작동한다.

### 2.2 리소스 경로 구조

Angular 앱은 상대 경로로 콘텐츠를 로드한다:

```
viewer/contents/index.html
  └── contentUrl: "../../resource/ebook/&page=..."
  
viewer/ebook/index.html
  └── app.config.json: "content": {"url": "./books/ebook"}
```

→ Docker 빌드 시 원본 디렉토리 구조를 그대로 유지해야 한다.

### 2.3 EPUB3 콘텐츠 구조

```
resource/contents/
├── 1/
│   ├── lesson01/OPS/
│   │   ├── content.opf        # EPUB3 메타데이터
│   │   ├── page-info.json     # 페이지 구조
│   │   ├── text/*.xhtml       # 콘텐츠 (XHTML)
│   │   ├── image/*            # 이미지
│   │   ├── video/*.mp4        # 동영상
│   │   └── audio/*.mp3        # 오디오
│   ├── lesson03/OPS/
│   ├── lesson05/OPS/
│   └── lesson06/OPS/
```

## 3. GitHub 유사 프로젝트 분석

### 3.1 BeePub (`oalieno/beepub`) — 가장 유사

| 항목 | 내용 |
|------|------|
| **목적** | Self-hosted EPUB 리더 (라이브러리 관리 + 뷰어) |
| **기술 스택** | Go backend + React frontend + nginx + PostgreSQL + Redis |
| **Docker 구조** | docker-compose: 5컨테이너 |
| **nginx 역할** | 정적 커버 이미지 직접 서빙, API 프록시, SPA 프록시 |
| **학습 포인트** | `location /covers/ { alias /data/covers/; expires 7d; }` 패턴 |

### 3.2 Calibre-Web-Automated (`crocodilestick/Calibre-Web-Automated`)

| 항목 | 내용 |
|------|------|
| **목적** | Calibre 도서관 웹 UI + 자동 다운로드 |
| **기술 스택** | Python (Calibre-Web) + Calibre CLI + Docker |
| **ARM64 지원** | `linux/arm64` 이미지 제공 |
| **학습 포인트** | 단일 컨테이너로 뷰어 + 메타데이터 관리 통합 |

### 3.3 비교 분석

| 프로젝트 | 뷰어 유형 | DB 필요 | ARM64 | 복잡도 |
|----------|----------|---------|-------|--------|
| BeePub | React SPA | PostgreSQL + Redis | 미확인 | 높음 |
| Calibre-Web | Python UI | Calibre DB | ✅ | 중간 |
| **etextbook** | **Angular SPA** | **불필요** | **✅** | **낮음** |

**etextbook의 장점**: 백엔드/DB 불필요. 정적 파일 서빙만으로 충분.

## 4. 기술 검증 결과

### 4.1 nginx:alpine ARM64 호환성

| 검증 항목 | 결과 | 근거 |
|----------|------|------|
| `nginx:alpine` ARM64 지원 | ✅ | Docker Hub `arm64v8/nginx` 공식 이미지 |
| 플랫폼 | `linux/arm64/v8` | `docker manifest inspect nginx:alpine` 확인 |
| 이미지 크기 | ~40MB | Alpine 기반 경량 이미지 |
| 대안 | `yobasystems/alpine-nginx` | aarch64 전용 태그 제공 |

### 4.2 OCI Ampere A1 호환성

| 검증 항목 | 결과 | 근거 |
|----------|------|------|
| 프로세서 | Ampere Altra (ARM64) | OCI 공식 문서 |
| 무료 티어 | 3,000 OCPU-hours + 18,000 GB-hours/월 | oracle.com/cloud/free |
| Docker 지원 | ✅ | 다수 블로그/튜토리얼 검증 |
| nginx 성능 | x86 대비 46% 향상 | Oracle 공식 벤치마크 |

### 4.3 Angular SPA nginx 라우팅

| 검증 항목 | 결과 | 근거 |
|----------|------|------|
| `try_files` 패턴 | `try_files $uri $uri/ /index.html;` | StackOverflow 다수 검증 |
| 커스텀 base href | `location /app/ { try_files $uri $uri/ /app/index.html; }` | dev.to 검증 |
| `=404` 옵션 | 리다이렉트 루프 방지 | 서버FAULT 검증 |

### 4.4 EPUB3 MIME 타입

| 검증 항목 | 결과 | 근거 |
|----------|------|------|
| XHTML MIME 타입 | `text/html` 사용 (호환 모드) | W3C XHTML 호환 가이드 |
| `application/xhtml+xml` | IE 미지원으로 비실용 | W3C 문서 |
| 미디어 타입 | mp4=video/mp4, mp3=audio/mpeg | 표준 MIME 타입 |

### 4.5 nginx 정적 파일 최적화

| 검증 항목 | 결과 | 근거 |
|----------|------|------|
| `sendfile on` | 커널 레벨 파일 전송 | nginx 공식 문서 |
| `tcp_nopush on` | 패킷 최적화 | nginx 공식 문서 |
| `open_file_cache` | 파일 디스크립터 캐시 | reintech.io 벤치마크 |
| `expires` 헤더 | 브라우저 캐싱 | BeePub 패턴 |

## 5. Docker 아키텍처

### 5.1 Dockerfile

```dockerfile
FROM nginx:alpine

# nginx 설정 복사
COPY nginx.conf /etc/nginx/nginx.conf

# 정적 파일 복사 (원본 디렉토리 구조 유지)
COPY viewer/ /usr/share/nginx/html/viewer/
COPY resource/ /usr/share/nginx/html/resource/

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
```

**설명**:
- 멀티스테이지 빌드 불필요 (정적 파일 복사만)
- `nginx:alpine` = ~40MB (ARM64 자동 선택)
- 콘텐츠 = ~250MB
- **총 이미지 크기: ~290MB**

### 5.2 nginx.conf

```nginx
events {
    worker_connections 1024;
}

http {
    include       /etc/nginx/mime.types;
    default_type  application/octet-stream;

    # 성능 최적화
    sendfile on;
    tcp_nopush on;
    tcp_nodelay on;

    # 파일 디스크립터 캐시
    open_file_cache max=1000 inactive=20s;
    open_file_cache_valid 30s;
    open_file_cache_min_uses 2;
    open_file_cache_errors on;

    keepalive_timeout 65;

    server {
        listen 80;
        root /usr/share/nginx/html;
        index index.html;

        # Angular SPA 라우팅 (viewer/contents)
        location /viewer/contents/ {
            try_files $uri $uri/ /viewer/contents/index.html =404;
        }

        # Angular SPA 라우팅 (viewer/ebook)
        location /viewer/ebook/ {
            try_files $uri $uri/ /viewer/ebook/index.html =404;
        }

        # EPUB 리소스 캐싱
        location /resource/ {
            expires 30d;
            add_header Cache-Control "public, immutable";
        }

        # 정적 파일 캐싱 (JS, CSS, 이미지)
        location ~* \.(js|css|png|jpg|jpeg|gif|ico|svg|woff2?)$ {
            expires 7d;
            add_header Cache-Control "public, immutable";
            access_log off;
        }

        # 미디어 파일 (mp4, mp3)
        location ~* \.(mp4|mp3)$ {
            expires 30d;
            add_header Cache-Control "public, immutable";
        }

        # 기본 SPA fallback
        location / {
            try_files $uri $uri/ /index.html =404;
        }
    }
}
```

### 5.3 이미지 크기 분석

| 컴포넌트 | 크기 |
|----------|------|
| nginx:alpine (ARM64) | ~40MB |
| viewer/ (SPA + JS + CSS) | ~50MB |
| resource/contents/ (EPUB3, 4과) | ~200MB |
| **총 이미지** | **~290MB** |

### 5.4 리소스 요구사항

| 항목 | 요구량 | OCI A1.Flex (2 OCPU / 8GB) |
|------|--------|---------------------------|
| 디스크 | ~300MB | 47GB 부트 볼륨 충분 |
| 메모리 (nginx) | ~50MB | 8GB 중 ~1% 사용 |
| CPU | minimal | 2 OCPU 충분 |

## 6. 배포 단계

### 6.1 Azure Blob에서 파일 추출

```bash
# Azure CLI 사용 (또는 azcopy)
azcopy copy \
  "https://stshareddevforgeprodkrc.blob.core.windows.net/etextbook-2026/middle/pe/app/viewer/*" \
  "./viewer/" --recursive

azcopy copy \
  "https://stshareddevforgeprodkrc.blob.core.windows.net/etextbook-2026/middle/pe/app/resource/*" \
  "./resource/" --recursive
```

### 6.2 Docker 이미지 빌드

```bash
# OCI 인스턴스에서 직접 빌드 (ARM64 네이티브)
docker build -t etextbook-viewer:latest .

# 또는 로컬에서 빌드 후 푸시
docker buildx build --platform linux/arm64 -t etextbook-viewer:latest .
```

### 6.3 컨테이너 실행

```bash
docker run -d \
  --name etextbook \
  --restart unless-stopped \
  -p 80:80 \
  etextbook-viewer:latest
```

### 6.4 OCI 네트워크 설정

1. VCN 보안그룹에서 포트 80 ingress 허용
2. 인스턴스 보안그룹에서 포트 80 허용
3. (선택) 도메인 연결 시 Let's Encrypt + Caddy/수동 설정

## 7. 검증 방법

### 7.1 로컬 테스트

```bash
# Docker 로컬 실행
docker run -d -p 8080:80 etextbook-viewer:latest

# 브라우저 접속 확인
curl http://localhost:8080/viewer/contents/
curl http://localhost:8080/viewer/ebook/
```

### 7.2 배포 후 검증

```bash
# 컨테이너 상태 확인
docker ps | grep etextbook

# 로그 확인
docker logs etextbook

# 리소스 확인
docker stats etextbook
```

## 8. 고려사항

### 8.1 보안

| 항목 | 현재 상태 | 권장 사항 |
|------|----------|----------|
| 인증 | 없음 | 추가 필요 여부 확인 |
| HTTPS | 없음 | Let's Encrypt 또는 OCI 로드밸런서 |
| 접근 제어 | 없음 | IP 기반 또는 nginx basic auth |

### 8.2 확장성

| 항목 | 현재 계획 | 향후 확장 |
|------|----------|----------|
| 뷰어 | 단일 컨테이너 | 다중 레플리카 (OCI LB) |
| 콘텐츠 | 빌드 시 포함 | 볼륨 마운트로 분리 |
| 도메인 | IP 직접 접근 | 커스텀 도메인 + HTTPS |

### 8.3 문제점 및 대응

| 문제 | 대응 |
|------|------|
| base href 불일치 | Dockerfile에서 index.html 수정 |
| 대용량 미디어 (mp4) | `limit_rate_after`로 대역폭 제한 |
| 동시 접속자 | nginx `worker_connections` 조정 |

## 9. 다음 단계

| # | 작업 | 상태 | 예상 시간 |
|---|------|------|----------|
| 1 | GitHub 유사 프로젝트 보고서 | ✅ 완료 | - |
| 2 | Docker 아키텍처 설계 | ✅ 완료 | - |
| 3 | 기술 검증 (web search) | ✅ 완료 | - |
| 4 | **사용자 검토 및 결정** | ⬅️ **현재** | - |
| 5 | Azure Blob에서 파일 추출 | 대기 | 10분 |
| 6 | Dockerfile + nginx.conf 작성 | 대기 | 5분 |
| 7 | OCI 인스턴스 생성/접속 | 대기 | 15분 |
| 8 | Docker 빌드 → 배포 → 테스트 | 대기 | 20분 |

## 10. 결정이 필요한 사항

1. **도메인/HTTPS**: OCI에서 자체 도메인 사용? Let's Encrypt 설정? 또는 IP 직접 접근?
2. **인증**: 뷰어에 로그인 기능이 현재 없음 — 추가 필요?
3. **범위**: 4과 전체를 하나의 이미지에 포함? 아니면 과별 분리?
4. **OCI 인스턴스**: 기존 인스턴스가 있는지, 새로 생성?

---

*문서 생성: 2026-08-28 | 검증 완료: web search 6건*

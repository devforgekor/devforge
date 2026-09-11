
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

> ⚠️ **업로드 현황 주의**: 위 수치는 **체육(pe) 1과목만 업로드 완료된 상태**의 실측값이다(`middle/pe/app` 7,265개 파일 ~7.02GB + `middle/pe/run` 127개 ~128MB 등). 다른 과목은 아직 업로드되지 않았으며, 향후 과목이 추가되면 5.3~5.4절의 용량 산정을 다시 계산해야 한다.

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

> ⚠️ **변경**: 콘텐츠(`resource/`)를 이미지에 `COPY`로 굽는 대신, **호스트 볼륨 마운트** 방식으로 변경한다. 콘텐츠가 이미지에 baked-in되면 과목 추가/개정 시마다 이미지 재빌드가 필요해, 앞서 결정된 "Azure Blob 상시 보관 + OCI On-Demand/rclone 과목 단위 복제" 운영 방향과 충돌하기 때문이다.

```dockerfile
FROM nginx:alpine

# nginx 설정 복사
COPY nginx.conf /etc/nginx/nginx.conf

# 정적 파일 복사 (viewer는 변경이 적어 이미지에 포함)
COPY viewer/ /usr/share/nginx/html/viewer/

# resource/(EPUB3 콘텐츠)는 이미지에 포함하지 않음 — 아래 6.3절처럼
# 호스트 볼륨(/data/etextbook)을 컨테이너 실행 시 마운트한다.

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
```

**설명**:
- 멀티스테이지 빌드 불필요 (정적 파일 복사만)
- `nginx:alpine` = ~40MB (ARM64 자동 선택)
- `viewer/` (이미지에 포함) = ~50MB
- **총 이미지 크기: ~90MB** (콘텐츠는 볼륨 마운트이므로 이미지 크기에서 제외)
- 실제 콘텐츠(`resource/`)는 실측 기준 pe 1과목만 해도 ~7GB이므로 이미지에 포함하지 않는 것이 타당함 (5.3절 참고)

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

### 5.3 이미지 크기 분석 (실측 반영)

> ⚠️ 이전 버전의 "resource/contents(4과) ~200MB"는 실측(`middle/pe/app` 7,265개 파일 ~7.02GB)과 **약 35배 차이**가 있던 값으로, 실제 콘텐츠 용량을 아래와 같이 재산정한다. 콘텐츠는 5.1절 결정에 따라 이미지가 아닌 볼륨으로 분리한다.

| 컴포넌트 | 크기 | 포함 위치 |
|----------|------|----------|
| nginx:alpine (ARM64) | ~40MB | 이미지 |
| viewer/ (SPA + JS + CSS) | ~50MB | 이미지 |
| **총 이미지** | **~90MB** | - |
| resource/(pe 1과목, 실측) | ~7.02GB | 호스트 볼륨(`/data/etextbook`) |
| 향후 과목 추가 시 | 과목당 +~7~10GB | 호스트 볼륨 |

### 5.4 리소스 요구사항 (실측 서버 사양 반영)

> ⚠️ 실제 OCI `onmydoc` 인스턴스 사양은 **2 OCPU / 12GB 메모리, 부트 볼륨 45GB(가용 42GB), 별도 블록 볼륨 미생성**으로 확인됨 (문서 초안의 "8GB/47GB"와 다름).

| 항목 | 요구량 | 실제 OCI `onmydoc` (2 OCPU / 12GB) |
|------|--------|---------------------------|
| 디스크(이미지) | ~90MB | 부트 볼륨(가용 42GB) 내 충분 |
| 디스크(콘텐츠, 볼륨) | 과목당 ~7~10GB | **별도 블록 볼륨 필요** — 부트 볼륨 42GB만으로는 과목 4~5개 누적 시 부족 |
| 메모리 (nginx) | ~50MB | 12GB 중 <1% 사용 |
| CPU | minimal | 2 OCPU 충분 |

## 6. 배포 단계

### 6.0 사전 준비 (신규 — 실측 확인 결과 반영)

> ⚠️ 실측 확인 결과 OCI `onmydoc` 인스턴스에는 **Docker가 설치되어 있지 않고, 별도 블록 볼륨도 생성되어 있지 않다.** 아래 단계를 6.1 이전에 반드시 먼저 수행한다.

```bash
# (a) 블록 볼륨 생성 및 부착 (OCI 콘솔 또는 CLI) 후, 인스턴스에서:
sudo mkfs.ext4 /dev/sdb   # 실제 부착된 디바이스명 확인 필요
sudo mkdir -p /data/etextbook
sudo mount /dev/sdb /data/etextbook
echo '/dev/sdb /data/etextbook ext4 defaults 0 2' | sudo tee -a /etc/fstab

# (b) Docker Engine 설치 (Oracle Linux 기준)
sudo dnf install -y dnf-utils zip unzip
sudo dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io
sudo systemctl enable --now docker
```

### 6.1 Azure Blob에서 파일 추출

```bash
# Azure CLI 사용 (또는 azcopy)
azcopy copy \
  "https://stshareddevforgeprodkrc.blob.core.windows.net/etextbook-2026/middle/pe/app/viewer/*" \
  "./viewer/" --recursive

# resource/(콘텐츠)는 이미지가 아닌 블록 볼륨(/data/etextbook)으로 직접 추출
azcopy copy \
  "https://stshareddevforgeprodkrc.blob.core.windows.net/etextbook-2026/middle/pe/app/resource/*" \
  "/data/etextbook/resource/" --recursive
```

### 6.2 Docker 이미지 빌드

```bash
# OCI 인스턴스에서 직접 빌드 (ARM64 네이티브)
docker build -t etextbook-viewer:latest .

# 또는 로컬에서 빌드 후 푸시
docker buildx build --platform linux/arm64 -t etextbook-viewer:latest .
```

### 6.3 컨테이너 실행 (볼륨 마운트 방식)

```bash
docker run -d \
  --name etextbook \
  --restart unless-stopped \
  -p 80:80 \
  -v /data/etextbook/resource:/usr/share/nginx/html/resource \
  etextbook-viewer:latest
```

### 6.4 OCI 네트워크 설정

1. VCN 보안그룹에서 포트 80 ingress 허용
2. 인스턴스 보안그룹에서 포트 80 허용
3. (선택) 도메인 연결 시 Let's Encrypt + Caddy/수동 설정

### 6.5 접근 제어 (필수 작업)

저작권이 있는 교육부/발행사 콘텐츠를 인증 없이 인터넷에 공개하는 것은 라이선스 위반 위험이 있으므로, 아래 중 최소 하나를 배포 전 필수로 적용한다.

- nginx `basic auth` (`htpasswd` 기반) 또는
- IP allowlist (학교/교육청 네트워크 대역만 `allow`, 나머지는 `deny all`)
- 가능하면 최소한의 HTTPS(자체 서명 인증서 또는 OCI 로드밸런서 무료 인증서)도 함께 적용

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
| 인증/접근 제어 | 없음 | **필수** — nginx basic auth 또는 IP allowlist (6.5절) |
| HTTPS | 없음 | Let's Encrypt 또는 OCI 로드밸런서 (권장) |

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
| 4 | **사용자 검토 및 결정** | ✅ 완료 | - |
| 5 | 블록 볼륨 생성/부착 + Docker 설치 (6.0) | 대기 | 15분 |
| 6 | Azure Blob에서 파일 추출 | 대기 | 10분 |
| 7 | Dockerfile(볼륨 마운트 방식) + nginx.conf 작성 | 대기 | 5분 |
| 8 | 접근 제어(basic auth/IP allowlist) 설정 (6.5) | 대기 | 10분 |
| 9 | Docker 빌드 → 배포 → 테스트 | 대기 | 20분 |

## 10. 결정이 필요한 사항

1. **도메인/HTTPS**: OCI에서 자체 도메인 사용? Let's Encrypt 설정? 또는 IP 직접 접근?
2. **인증**: (해결됨) 6.5절대로 basic auth 또는 IP allowlist를 필수 적용 — 방식만 최종 확정 필요.
3. **범위**: (해결됨) 콘텐츠를 볼륨 마운트로 분리했으므로 이미지는 단일 유지, 볼륨 안에서 과목 폴더만 추가/삭제.
4. **OCI 인스턴스**: 기존 `onmydoc` 인스턴스(ap-tokyo-1, A1.Flex, 2 OCPU/12GB) 재사용 — 블록 볼륨 신규 부착 필요.

---

*문서 생성: 2026-08-28 | 검증 완료: web search 6건 | v2 반영: 서버 사양 실측/보안 필수화/볼륨 마운트 전환*

-- Golden Image schema — docs/runbook-golden-image.md §4.3
-- PostgreSQL 16, devforge_app, 멱등(IF NOT EXISTS)

-- 1. golden_image_versions — 버전, 이미지ID, 상태(active/deprecated), 메모
CREATE TABLE IF NOT EXISTS golden_image_versions (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    version     TEXT NOT NULL UNIQUE,  -- YYYY.MM.0 예: 2026.02.0
    image_id    TEXT,                  -- Gallery image version resource ID
    status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','deprecated')),
    memo        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_golden_versions_status ON golden_image_versions(status);
CREATE INDEX IF NOT EXISTS idx_golden_versions_created ON golden_image_versions(created_at DESC);

-- 2. deployment_logs — 배포 이력, VM명, 상태, 공인IP, 에러
CREATE TABLE IF NOT EXISTS deployment_logs (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    vm_name     TEXT NOT NULL,
    version     TEXT REFERENCES golden_image_versions(version),
    status      TEXT NOT NULL CHECK (status IN ('pending','running','success','failed','evicted')),
    public_ip   TEXT,
    error       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_deployment_status ON deployment_logs(status);
CREATE INDEX IF NOT EXISTS idx_deployment_created ON deployment_logs(created_at DESC);

-- 3. health_checks — 헬스체크 결과 (성공/실패, 레이턴시)
CREATE TABLE IF NOT EXISTS health_checks (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    deployment_id   BIGINT REFERENCES deployment_logs(id) ON DELETE CASCADE,
    success         BOOLEAN NOT NULL,
    latency_ms      INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_health_deployment ON health_checks(deployment_id);
CREATE INDEX IF NOT EXISTS idx_health_created ON health_checks(created_at DESC);

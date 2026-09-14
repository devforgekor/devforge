-- ============================================================
-- DevForge DB Schema — application-owned tables
-- 적용 대상: devforge_app (PostgreSQL 16)
-- 정본: src/devforge/domain/models.py (SQLAlchemy) 와 일치
-- 갱신: 2026-09-14
-- 참고: 라이브 DB에는 다른 서브시스템(news/ebook/calendar 등) 소유 테이블이
--       추가로 존재한다. 이 파일은 devforge 앱 소유 16개 테이블만 문서화한다.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ── Tables ──
CREATE TABLE activity_log (
	id SERIAL NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	type TEXT NOT NULL, 
	source TEXT NOT NULL, 
	title TEXT NOT NULL, 
	summary TEXT DEFAULT '' NOT NULL, 
	agent TEXT, 
	model TEXT, 
	body JSONB DEFAULT '{}', 
	tags TEXT[] DEFAULT '{}', 
	git_commit_hash TEXT, 
	run_id TEXT, 
	trace_id TEXT, 
	parent_id INTEGER, 
	turn_ids UUID[] DEFAULT '{}', 
	summary_status TEXT DEFAULT 'raw' NOT NULL, 
	queue_status TEXT DEFAULT 'unprocessed' NOT NULL, 
	exec_status TEXT DEFAULT 'DONE' NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE conversations (
	id UUID NOT NULL, 
	title TEXT, 
	source TEXT NOT NULL, 
	model TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE deepdive_steps (
	id UUID NOT NULL, 
	session_id TEXT NOT NULL, 
	step INTEGER NOT NULL, 
	step_name TEXT NOT NULL, 
	base_timeout_sec INTEGER NOT NULL, 
	min_bound_sec INTEGER NOT NULL, 
	max_bound_sec INTEGER NOT NULL, 
	started_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	ended_at TIMESTAMP WITH TIME ZONE, 
	elapsed_sec INTEGER, 
	last_heartbeat_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	overrun_count INTEGER DEFAULT 0 NOT NULL, 
	status TEXT DEFAULT 'ACTIVE' NOT NULL, 
	affected_files INTEGER, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE embeddings (
	id UUID NOT NULL, 
	source_type TEXT NOT NULL, 
	source_id UUID NOT NULL, 
	embed_text TEXT NOT NULL, 
	embedding vector(2048), 
	model_name TEXT DEFAULT 'qwen3-embedding-8b-v1' NOT NULL, 
	chunk_index INTEGER DEFAULT 0 NOT NULL, 
	metadata JSONB DEFAULT '{}' NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE golden_image_versions (
	id SERIAL NOT NULL, 
	version TEXT NOT NULL, 
	image_id TEXT, 
	status TEXT DEFAULT 'active' NOT NULL, 
	memo TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (version)
);

CREATE TABLE observations (
	id UUID NOT NULL, 
	observation TEXT NOT NULL, 
	category TEXT DEFAULT 'general' NOT NULL, 
	source TEXT DEFAULT 'qwen_worker' NOT NULL, 
	context JSONB DEFAULT '{}', 
	tags JSONB DEFAULT '{}', 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE reflex_rules (
	id UUID NOT NULL, 
	trigger_category TEXT, 
	trigger_source TEXT, 
	trigger_tags JSONB DEFAULT '{}', 
	trigger_pattern TEXT, 
	trigger_min_count INTEGER DEFAULT 1, 
	trigger_window_hours INTEGER DEFAULT 24, 
	action_type TEXT NOT NULL, 
	action_params JSONB DEFAULT '{}', 
	confidence FLOAT DEFAULT 0.0, 
	status TEXT DEFAULT 'candidate' NOT NULL, 
	description TEXT, 
	rationale TEXT, 
	observation_count INTEGER DEFAULT 0, 
	last_matched_at TIMESTAMP WITH TIME ZONE, 
	last_applied_at TIMESTAMP WITH TIME ZONE, 
	supersedes UUID, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(supersedes) REFERENCES reflex_rules (id)
);

CREATE TABLE watchdog_incidents (
	id SERIAL NOT NULL, 
	dedup_key TEXT NOT NULL, 
	component TEXT NOT NULL, 
	status TEXT DEFAULT 'open' NOT NULL, 
	symptom TEXT, 
	context TEXT, 
	detected_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	last_seen_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	action TEXT, 
	action_result TEXT, 
	action_at TIMESTAMP WITH TIME ZONE, 
	resolved_at TIMESTAMP WITH TIME ZONE, 
	fail_count INTEGER DEFAULT 1 NOT NULL, 
	reopen_count INTEGER DEFAULT 0 NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE worklog_entries (
	id SERIAL NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	date DATE NOT NULL, 
	title TEXT NOT NULL, 
	summary TEXT NOT NULL, 
	status TEXT DEFAULT 'done' NOT NULL, 
	kind TEXT DEFAULT 'task' NOT NULL, 
	details JSONB DEFAULT '[]', 
	files JSONB DEFAULT '[]', 
	tags TEXT[] DEFAULT '{}', 
	agent TEXT, 
	model TEXT, 
	turn_ids UUID[] DEFAULT '{}', 
	PRIMARY KEY (id)
);

CREATE TABLE deployment_logs (
	id SERIAL NOT NULL, 
	vm_name TEXT NOT NULL, 
	version TEXT, 
	status TEXT NOT NULL, 
	public_ip TEXT, 
	error TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(version) REFERENCES golden_image_versions (version)
);

CREATE TABLE mcp_dec (
	id UUID NOT NULL, 
	conversation_id UUID, 
	summary TEXT, 
	detail TEXT, 
	turn_ids UUID[] DEFAULT '{}', 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(conversation_id) REFERENCES conversations (id) ON DELETE CASCADE
);

CREATE TABLE turns (
	id UUID NOT NULL, 
	conversation_id UUID NOT NULL, 
	seq INTEGER NOT NULL, 
	user_turn TEXT NOT NULL, 
	thinking TEXT, 
	text TEXT NOT NULL, 
	meta JSONB DEFAULT '{}' NOT NULL, 
	wing TEXT, 
	room TEXT, 
	agent TEXT, 
	source_message_id TEXT, 
	embedding vector(768), 
	pipeline_state TEXT DEFAULT 'scanned' NOT NULL, 
	source TEXT DEFAULT 'unknown', 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(conversation_id) REFERENCES conversations (id) ON DELETE CASCADE
);

CREATE TABLE file_registry (
	id UUID NOT NULL, 
	filename TEXT NOT NULL, 
	path TEXT NOT NULL, 
	size INTEGER, 
	hash TEXT, 
	mime_type TEXT, 
	source TEXT NOT NULL, 
	description TEXT, 
	tags TEXT[] DEFAULT '{}', 
	turn_id UUID, 
	blob_url TEXT, 
	sender TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(turn_id) REFERENCES turns (id)
);

CREATE TABLE health_checks (
	id SERIAL NOT NULL, 
	deployment_id INTEGER, 
	success BOOLEAN NOT NULL, 
	latency_ms INTEGER, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(deployment_id) REFERENCES deployment_logs (id) ON DELETE CASCADE
);

CREATE TABLE obs_dec (
	id UUID NOT NULL, 
	turn_id UUID NOT NULL, 
	decision TEXT NOT NULL, 
	rationale TEXT, 
	context TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (turn_id), 
	FOREIGN KEY(turn_id) REFERENCES turns (id) ON DELETE CASCADE
);

CREATE TABLE review_facts (
	id UUID NOT NULL, 
	turn_id UUID NOT NULL, 
	fact_index INTEGER NOT NULL, 
	fact_type TEXT NOT NULL, 
	evidence TEXT NOT NULL, 
	extract_model TEXT NOT NULL, 
	verdict TEXT DEFAULT 'passed', 
	source TEXT NOT NULL, 
	fact_action TEXT DEFAULT 'extracted', 
	prompt_tokens INTEGER, 
	gen_tokens INTEGER, 
	elapsed_ms FLOAT, 
	faithful_score FLOAT, 
	faithful_method TEXT, 
	nli_verdict TEXT, 
	nli_llm TEXT, 
	nli_llm2 TEXT, 
	source_file TEXT, 
	corrected_evidence TEXT, 
	subject TEXT, 
	predicate TEXT, 
	object TEXT, 
	qualifiers JSONB DEFAULT '{}', 
	quality_checks JSONB DEFAULT '{}', 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_review_facts_turn_fact_model UNIQUE (turn_id, fact_index, extract_model), 
	FOREIGN KEY(turn_id) REFERENCES turns (id) ON DELETE CASCADE
);

-- ── Indexes ──
CREATE INDEX idx_activity_source ON activity_log (source);
CREATE INDEX idx_activity_commit ON activity_log (git_commit_hash) WHERE git_commit_hash IS NOT NULL;
CREATE INDEX idx_activity_tags ON activity_log USING gin (tags);
CREATE UNIQUE INDEX idx_activity_commit_unique ON activity_log (git_commit_hash) WHERE git_commit_hash IS NOT NULL AND type = 'commit';
CREATE INDEX idx_activity_trace ON activity_log (trace_id) WHERE trace_id IS NOT NULL;
CREATE INDEX idx_activity_body_gin ON activity_log USING gin (body);
CREATE INDEX idx_activity_queue ON activity_log (queue_status, created_at) WHERE queue_status = 'unprocessed';
CREATE INDEX idx_activity_created ON activity_log (created_at DESC);
CREATE INDEX idx_activity_run ON activity_log (run_id) WHERE run_id IS NOT NULL;
CREATE INDEX idx_activity_type ON activity_log (type);
CREATE INDEX idx_activity_parent ON activity_log (parent_id) WHERE parent_id IS NOT NULL;
CREATE UNIQUE INDEX idx_activity_stage_unique ON activity_log (run_id, type, parent_id) WHERE run_id IS NOT NULL AND type = 'stage' AND exec_status = 'DONE';
CREATE INDEX idx_deepdive_steps_session ON deepdive_steps (session_id);
CREATE INDEX idx_deepdive_steps_status ON deepdive_steps (status, started_at);
CREATE UNIQUE INDEX uq_deepdive_session_step ON deepdive_steps (session_id, step);
CREATE UNIQUE INDEX idx_embeddings_unique ON embeddings (source_type, source_id, model_name, chunk_index);
CREATE INDEX idx_embeddings_source_chunk ON embeddings (source_type, source_id, chunk_index);
CREATE INDEX idx_embeddings_source ON embeddings (source_type, source_id);
CREATE INDEX idx_golden_versions_status ON golden_image_versions (status);
CREATE INDEX idx_golden_versions_created ON golden_image_versions (created_at DESC);
CREATE INDEX idx_observations_trgm ON observations USING gin (observation gin_trgm_ops);
CREATE INDEX idx_observations_category ON observations (category);
CREATE INDEX idx_observations_tags ON observations USING gin (tags);
CREATE INDEX idx_observations_created ON observations (created_at DESC);
CREATE INDEX idx_reflex_rules_updated ON reflex_rules (updated_at DESC);
CREATE INDEX idx_reflex_rules_tags ON reflex_rules USING gin (trigger_tags);
CREATE INDEX idx_reflex_rules_status ON reflex_rules (status);
CREATE INDEX idx_reflex_rules_pattern ON reflex_rules USING gin (trigger_pattern gin_trgm_ops);
CREATE INDEX idx_reflex_rules_trigger ON reflex_rules (trigger_category, trigger_source);
CREATE INDEX idx_watchdog_incidents_created ON watchdog_incidents (detected_at DESC);
CREATE INDEX idx_watchdog_incidents_open ON watchdog_incidents (status, dedup_key);
CREATE UNIQUE INDEX idx_worklog_unique ON worklog_entries (date, title);
CREATE UNIQUE INDEX idx_worklog_one_in_progress ON worklog_entries ((status)) WHERE status = 'in_progress';
CREATE INDEX idx_worklog_tags ON worklog_entries USING gin (tags);
CREATE INDEX idx_worklog_date ON worklog_entries (date DESC);
CREATE INDEX idx_deployment_status ON deployment_logs (status);
CREATE INDEX idx_deployment_created ON deployment_logs (created_at DESC);
CREATE INDEX idx_turns_pipeline_state ON turns (pipeline_state);
CREATE INDEX idx_turns_conversation ON turns (conversation_id, seq);
CREATE INDEX idx_turns_search ON turns USING gin ((COALESCE(user_turn, '') || ' ' || COALESCE(text, '') || ' ' || COALESCE(thinking, '')) gin_trgm_ops);
CREATE UNIQUE INDEX idx_turns_source_msg ON turns (source_message_id) WHERE source_message_id IS NOT NULL;
CREATE INDEX idx_turns_meta_type ON turns ((meta->>'type'));
CREATE INDEX idx_turns_created ON turns (created_at DESC);
CREATE INDEX idx_file_registry_created ON file_registry (created_at DESC);
CREATE INDEX idx_file_registry_filename_trgm ON file_registry USING gin (filename gin_trgm_ops);
CREATE INDEX idx_file_registry_tags ON file_registry USING gin (tags);
CREATE INDEX idx_file_registry_source ON file_registry (source);
CREATE INDEX idx_file_registry_desc_trgm ON file_registry USING gin (description gin_trgm_ops);
CREATE INDEX idx_health_created ON health_checks (created_at DESC);
CREATE INDEX idx_health_deployment ON health_checks (deployment_id);
CREATE INDEX idx_review_type ON review_facts (fact_type);
CREATE INDEX idx_review_turn ON review_facts (turn_id);
CREATE INDEX idx_review_verdict ON review_facts (verdict);
CREATE INDEX idx_review_source ON review_facts (source);

# Seedling → DevForge Gemini Logic Selection Report

**Written:** 2026-05-15
**Target:** DevForge server infrastructure reinforcement for Gemini CLI operation
**Basis:** common-main.md based DevForge architecture (Podman rootless, PostgreSQL 16, lightweight MCP server)

---

## Selected Logic

### 1. AES-256-GCM Encryption (`app/security.py` — encrypt_data / decrypt_data)

**Reason for adoption:**
- Currently Gemini CLI stores `GEMINI_API_KEY` in `settings.json` plaintext or env vars
- DevForge has PostgreSQL, so encryption key can be managed via `ENCRYPTION_PASSPHRASE` and the API key body can be encrypted in storage
- PBKDF2-HMAC-SHA256 (100,000 iterations) → HKDF key derivation provides brute-force resistance
- Security level consistent with `common-rule.md`'s "Secrets: secrets.env (chmod 600) only" principle

**Application plan:**
```python
# Port to /opt/projects/server/lib/crypto.py
# ENCRYPTION_PASSPHRASE → injected from secrets.env
# Encrypt Gemini API key and store in DB or secrets file
```

**SLOC:** ~50 (2 functions + key derivation)

---

### 2. Circuit Breaker + Token Bucket (`app/circuit_breaker.py`)

**Reason for adoption:**
- DevForge depends on external APIs via LiteLLM — needs failure propagation prevention
- Gemini CLI itself retries internally, but scheduling CLI in headless mode wastes resources on consecutive failures
- 60-line lightweight implementation with zero dependencies
- Fault detection → fallback → auto-recovery pattern is universally useful

**Application plan:**
```python
# Port to /opt/projects/server/lib/circuit.py
# Circuit open on LiteLLM health check failure → alert
# Check circuit state before Gemini CLI cron execution
```

**SLOC:** ~60 (TokenBucket class + circuit_state dict)

---

### 3. Shared HTTP Client (`app/http_client.py`)

**Reason for adoption:**
- Eliminates inefficiency of creating new connections for every external API call from DevForge server
- TCP Keep-Alive + Connection Pool reduces latency
- `reset_client()` for built-in stale connection recovery pattern
- Only one dependency added: httpx (pure Python, aarch64 compatible)

**Application plan:**
```python
# Port to /opt/projects/server/lib/http_client.py
# Shared client for LiteLLM calls, health checks, external webhooks, etc.
```

**SLOC:** ~40

---

### 4. API Call Logging — Token Usage Tracking (`gemini_pool.py` — _log_api_call)

**Reason for adoption:**
- Gemini API billing is token-based, so usage monitoring is the foundation of cost management
- DevForge already has `devforge_app.worklog_entries` table → can add `api_call_logs` using the same pattern
- Importing the APICallLogs schema directly enables tracking by prompt/cached/candidates/thoughts token breakdown
- `last_token_detail` pattern for real-time per-session token usage visibility

**Application plan:**
```sql
-- Add api_call_logs table
CREATE TABLE IF NOT EXISTS api_call_logs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    model_used TEXT NOT NULL,
    token_count INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER DEFAULT 0,
    candidates_tokens INTEGER DEFAULT 0,
    cached_tokens INTEGER DEFAULT 0,
    thoughts_tokens INTEGER DEFAULT 0,
    service_tier TEXT DEFAULT 'default',
    status_code INTEGER NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

**SLOC:** ~30 (1 function + INSERT)

---

### 1. GeminiPoolManager — Partial Adoption as Lightweight KeyRotator (`app/gemini_pool.py`)

**Rejected portions (full PoolManager):**
- DB transactions + key state machine + pool_status/pool_keys tables + health_check → 600+ SLOC
- Keys groupable only by `pool_name`(lite/flash/gemma/combined) → cannot specify specific key indexes (e.g. #9, #10)
- `_SAFE_POOL_NAMES` hardcoded → cannot add new pools

**Selected portions (rotation + backoff):**
- round-robin + 429 detection + jitter backoff + daily quota isolation — sufficient reuse value
- In-memory implementation removes DB dependency, lightweight at ~60 SLOC
- Can rotate only specific keys

**Lightweight KeyRotator Design:**

```
Input: [("key9_name", "key9_value"), ("key10_name", "key10_value")]
Behavior:
  1. pick() → returns next key via round-robin (skip keys in backoff)
  2. success() → reset backoff/failure count
  3. rate_limited() → set backoff based on retry_delay
     - Per-minute quota (<5min) → temporary backoff with jitter
     - Daily quota (>=5min) → isolate until next day 17:00 KST
  4. Both in backoff → return earliest release time (wait)
```

**Estimated SLOC:** ~60 (No DB, in-memory dict only)

**Gemini CLI Integration:**
```bash
# KeyRotator selects one key before CLI execution → inject as GEMINI_API_KEY
export GEMINI_API_KEY=$(python3 /opt/projects/server/scripts/rotate_key.py pick)
gemini "$@"
python3 /opt/projects/server/scripts/rotate_key.py success  # or rate_limited <retry_sec>
```

---

### 2. LLM Client 2-Tier Model Routing (`app/llm_client.py` — call_gemini, generate_response)

**Reason for rejection:**
- flash-2.5 → gemma-4-26b fallback chain is for seedling's chatbot response generation
- Gemini CLI has its own model selection logic (`-m` flag, settings.json)
- Complexity classifier + thinking budget adjustment is specialized for conversational chatbot
- QDP/HyQE query decomposition, response cache, per-session adaptive promotion — all irrelevant to CLI

**SLOC:** 600+ — zero contribution to CLI operations relative to porting cost

---

### 3. PostgresKeyStore (`app/key_store.py` — entire module)

**Reason for rejection:**
- Full CRUD set (`add_key`, `remove_key`, `reactivate_key`, `migrate_from_env`) is unnecessary for a single key
- `model_type` classification (lite/flash/gemma), `account_prefix`-based distribution — multi-key/multi-model scenario only
- Encryption functions themselves are adopted (Selection #1), but full KeyStore abstraction is overkill

**Alternative:** Port only the encryption functions; simplify single key loading to `secrets.env` + `ENCRYPTION_PASSPHRASE` combination

---

### 4. Search Engine Routing (`app/search_engines.py`)

**Reason for rejection:**
- Gemini CLI has built-in `google_web_search` tool (no additional key required)
- Brave, Exa, Azure Bing, You.com — each requires a separate API key
- DevForge does not provide search functionality; built-in Gemini CLI tools are sufficient for searches

---

### 5. Prompt Builder + Complexity Classifier (`app/prompt_builder.py`, `app/complexity_classifier.py`)

**Reason for rejection:**
- seedling's chatbot response generation prompt engineering logic
- Gemini CLI manages GEMINI.md + system prompt itself
- Language detection, real-time question judgment, error type classification — all in conversational chatbot domain

---

### 6. Config (`app/config.py` — pydantic-settings)

**Reason for rejection:**
- DevForge uses a small number of environment variables (DB connection, LiteLLM URL, API key)
- Environment variable complexity is not high enough to justify adding pydantic-settings dependency
- Violates `common-rule.md`'s "Prefer stdlib before adding dependencies" principle
- Current `os.getenv` + `load_dotenv` is sufficient

---

## Priority Summary

| Priority | Logic | SLOC | Expected Effect |
|:---:|---|:---:|---|
| 1 | Encryption (encrypt/decrypt) | ~50 | API key security level improvement |
| 2 | **KeyRotator (lightweight rotation)** | ~60 | 2-key round-robin + 429 backoff |
| 3 | Circuit Breaker | ~60 | External API failure isolation |
| 4 | HTTP Client | ~40 | Connection pool reuse |
| 5 | API Call Logging | ~30 | Token usage/cost tracking |

**Total Estimated SLOC:** ~240 (No dependencies, pure stdlib + httpx)

---

## Adoption Recommendation

1. **Phase 1 (Immediate):** KeyRotator → Key #9·10 rotation, Gemini CLI integration
2. **Phase 2 (Immediate):** `encrypt_data`/`decrypt_data` → API key encrypted storage
3. **Phase 3 (Short-term):** Circuit Breaker + Token Bucket → Linked to LiteLLM health check
4. **Phase 4 (As needed):** HTTP Client → Introduce when external API calls increase
5. **Phase 5 (As needed):** API Call Logging → Introduce when cost monitoring is needed

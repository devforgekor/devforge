#!/usr/bin/env python3
# Status: experimental
# Path: none — 14B Q4_K_M x3 비교: Extract + NLI + MCP + Verify 4-phase
"""14B Q4_K_M 3종 비교: 실제 파이프라인 extract/verify 패턴으로 평가.
Phase A: Fact extraction (SYSTEM_DAY_EXTRACT, user_turn/thinking/text)
Phase B: 3-tier faithfulness (cosine embed → MiniCheck NLI)
Phase C: MCP metadata (SYSTEM_DAY_MCP, tldr/intent/entities/tags)
Phase D: Verify (verification report, global context)
"""
import argparse, json, os, re, subprocess, sys, time, urllib.request

SCRIPTS_DIR = "/opt/projects/server/scripts"
sys.path.insert(0, SCRIPTS_DIR)
os.environ["TOKENIZERS_PARALLELISM"] = "false"

from lib.llm_client import MODEL_REGISTRY
from lib.test_common import test_setup, test_heartbeat, test_complete, log, call_llm, parse_llm_json
from sentence_transformers import SentenceTransformer
from minicheck.minicheck import MiniCheck

# ── NLI/Embedding models ──
log_t0 = time.monotonic()
def log(m):
    elapsed = round(time.monotonic() - log_t0, 1)
    print(f"[{elapsed:>6.1f}s] {m}", flush=True)

log("Loading embedding model (all-MiniLM-L6-v2)...")
EMBED = SentenceTransformer('all-MiniLM-L6-v2')
log(f"  Embedding model ready")

log("Loading MiniCheck (roberta-large — lighter than flan-t5 for ARM)...")
t_mc = time.monotonic()
NLI = MiniCheck(model_name='roberta-large', cache_dir='/opt/ai_data/models')
mc_init_s = round(time.monotonic() - t_mc, 1)
log(f"  MiniCheck ready ({mc_init_s}s)")

# ── NLI Thresholds (matching production extract.py) ──
COSINE_FAITHFUL = 0.75
COSINE_UNFAITHFUL = 0.40
MC_THRESHOLD = 0.5      # MiniCheck faithful
MC_AMBIGUOUS = 0.3       # MiniCheck ambiguous

OUTPUT = "/tmp/14b_comparison_results.json"   # updated in __main__ for --rubric


# ═══════════════════════════════════════════════════════════════
# Production prompts (from pipelines/extract.py + day_verify_compare.py)
# ═══════════════════════════════════════════════════════════════

SYSTEM_DAY_EXTRACT = """\
You are a fact extractor for a developer-assistant conversation turn.
Each turn has three parts: user_turn (the user's message), thinking (the
model's internal reasoning, may be empty), and text (the model's response).

Extract key factual statements that are EXPLICITLY present in the text.
Do NOT infer, summarize, or add information not present in the source.

Output STRICT JSON:
{
  "extractions": [
    {
      "fact_type": "user|thinking|text",
      "evidence": "Exact quote or close paraphrase from the source",
      "category": "requirement|decision|explanation|code|reasoning|other"
    }
  ]
}

Rules:
- fact_type must match which source field the evidence came from
- evidence must be directly traceable to the source text
- Skip thinking if it is empty or contains only formatting
- Extract at most 5 facts per fact_type
- If nothing extractable, return {"extractions": []}"""

SYSTEM_DAY_MCP = """\
You are a conversation analyst preparing structured metadata for an MCP
(Model Context Protocol) system. Given the original conversation turn and
the extracted facts, produce structured MCP fields.

Output STRICT JSON:
{
  "tldr": "One-line summary (max 15 words) — what this turn is about",
  "intent": "question|request|report|clarification|code_change|debug|design|other",
  "entities": {
    "files": ["relative/file/path.py"],
    "technologies": ["Python", "FastAPI", ...],
    "functions": ["function_name"],
    "mentioned_users": []
  },
  "tags": ["tag1", "tag2"]
}

Rules:
- tldr must be factual and directly derivable from the turn content
- intent must be one of the enumerated values
- entities.files: only include file paths explicitly mentioned in the turn
- entities.technologies: programming languages, frameworks, tools mentioned
- entities.functions: function/class/method names mentioned
- tags: 2-5 keywords for discovery and routing
- If a field has no relevant data, use an empty array []"""

SYSTEM_VERIFY = """\
You are a final verifier. Review all extracted facts and MCP metadata.
Consider the global project context provided. Assess whether findings are
consistent with the known project state.

Return JSON:
{
  "final_verdict": "approved|rejected|escalate",
  "confidence": 0-100,
  "summary": "1-line summary of verification result",
  "reasoning": "2-3 sentences explaining the verdict",
  "verification_items": [
    {"check": "fact description", "result": "pass|fail|partial", "detail": "why"}
  ]
}"""

# ── Test 2: Rubric injection (Pro-designed 10-axis rubric from spec file) ──
USE_RUBRIC = False
RUBRIC_FULL = ""
_RUBRIC_PATH = os.path.join(SCRIPTS_DIR, "..", "docs", "specs", "14b-comparison-rubric-prompt.txt")
try:
    with open(_RUBRIC_PATH) as _rf:
        RUBRIC_FULL = _rf.read().strip()
except Exception:
    pass  # Test 1 — no rubric needed
_RUBRIC_LOADED = bool(RUBRIC_FULL)

# ═══════════════════════════════════════════════════════════════
# Global Project Context (시뮬레이션 — 실제 day_verify에 주입되는 정보)
# ═══════════════════════════════════════════════════════════════
GLOBAL_CONTEXT = """\
=== GLOBAL PROJECT CONTEXT (from day_verify MCP) ===
Project: devforge/server
Stack: Python 3.9, FastAPI, PostgreSQL 16 (pg_trgm), Caddy reverse proxy, blue-green deployment
Infrastructure: ARM Neoverse-N1 4-core, 22Gi RAM, Podman rootless
LLM: llama.cpp (Qwen models via :8082 extract / :8083 review)
DB Schema: turns(user_turn, thinking, text, created_at), review_facts(turn_id, fact_index, evidence, fact_type, category, faithful_score, faithful_method)
Known files: auth_routes.py, config.py, models/user.py, lib/db.py, lib/llm_client.py, pipelines/extract.py
CI/CD: GitHub Actions, code coverage 85%+, zero-downtime blue-green deploy via Caddy
Security: no secrets in repo, env vars via ~/.config/devforge/secrets.env, API keys rotated weekly
"""

# ═══════════════════════════════════════════════════════════════
# Test Cases — 실제 파이프라인 패턴을 반영한 6개 turn
# ═══════════════════════════════════════════════════════════════
GT_CASES = [
    # ── Case 1: 일반 fact 추출 (perf_bug) ──
    {
        "id": "extract_perf",
        "title": "Extract: 성능 이슈 fact 추출",
        "phase": "extract",
        "user_turn": "로그인 API가 3초나 걸리는데 원인이 뭘까? auth_routes.py 쪽인 것 같은데.",
        "thinking": "The user reports slow login at 3s. auth_routes.py is the suspect. Need to check if it's a DB connection issue or something else. Could be connection pooling missing since they mentioned new sessions per request.",
        "text": "/api/v1/auth/login 확인했습니다. auth_routes.py의 login() 함수가 매 요청마다 새로운 async DB 세션을 생성합니다. 연결 풀링이 전혀 적용되어 있지 않아 PostgreSQL 커넥션 핸드셰이크 비용이 매번 발생합니다. 이게 3초 응답의 주요 원인입니다. 해결책으로 SQLAlchemy connection pool 설정( pool_size=5, max_overflow=10 )을 추가하고, DB 세션을 의존성 주입 방식으로 변경하는 것을 추천합니다.",
        "ref_extractions": [
            {"fact_type": "text", "category": "explanation",
             "evidence": "auth_routes.py login() creates new async DB session per request, connection pooling not applied, causing 3s response time"},
            {"fact_type": "text", "category": "explanation",
             "evidence": "PostgreSQL connection handshake cost occurs every request"},
            {"fact_type": "text", "category": "decision",
             "evidence": "SQLAlchemy pool_size=5, max_overflow=10 recommended"},
        ],
        "ref_mcp": {
            "tldr": "Login API 3s latency caused by missing DB connection pooling in auth_routes.py",
            "intent": "debug",
            "entities": {"files": ["auth_routes.py"], "technologies": ["Python", "FastAPI", "PostgreSQL", "SQLAlchemy"], "functions": ["login"], "mentioned_users": []},
            "tags": ["performance", "database", "connection-pooling", "auth"],
        },
        "ref_verify": "approved",  # facts are consistent with project context (PostgreSQL 16)
        "hallu_trap": False,
    },
    # ── Case 2: 보안 이슈 (security_leak) ──
    {
        "id": "extract_security",
        "title": "Extract: 보안 이슈 fact 추출",
        "phase": "extract",
        "user_turn": "에러 로그에서 API 키가 발견됐어. 어떻게 해야 할까? 이거 심각한 문제인데.",
        "thinking": "This is critical. API keys in error logs means any developer with log access can see them. Retention is 90 days which makes it worse. Need to check what's being logged - Authorization header probably. Fix: sanitize logs and rotate keys.",
        "text": "/var/log/app/error.log 확인 결과, 인증 실패 시 stack trace와 함께 요청 파라미터 전체가 기록되고 있습니다. 특히 Authorization 헤더의 API key도 함께 로깅됩니다. 로그 보존 기간은 90일이며, 모든 개발자가 로그에 접근 가능한 상태입니다. 즉시 조치사항: (1) 로그에서 Authorization 헤더 마스킹 (2) 기존 로그에서 키 노출 부분 삭제 (3) API 키 전량 교체 (4) 로그 민감정보 필터 미들웨어 도입.",
        "ref_extractions": [
            {"fact_type": "text", "category": "explanation",
             "evidence": "API keys from Authorization header logged to /var/log/app/error.log on auth failure"},
            {"fact_type": "text", "category": "explanation",
             "evidence": "Log retention is 90 days, all developers have access"},
            {"fact_type": "text", "category": "decision",
             "evidence": "Actions: mask Authorization header in logs, delete exposed key entries, rotate all API keys, add sensitive data filter middleware"},
        ],
        "ref_mcp": {
            "tldr": "API keys exposed in error logs — requires immediate masking, log cleanup, and key rotation",
            "intent": "report",
            "entities": {"files": ["/var/log/app/error.log"], "technologies": ["Python"], "functions": [], "mentioned_users": []},
            "tags": ["security", "api-key", "logging", "p0"],
        },
        "ref_verify": "approved",  # consistent — no prior fix exists in global context
        "hallu_trap": False,
    },
    # ── Case 3: 이슈 없는 정상 turn (no_issue) ──
    {
        "id": "extract_clean",
        "title": "Extract: 정상 turn (이슈 없음)",
        "phase": "extract",
        "user_turn": "CI 파이프라인 통과했어. 배포해도 될까?",
        "thinking": "CI passed. Coverage 87%. All tests green. No blockers for deployment.",
        "text": "CI 파이프라인 전체 통과했습니다. 2분 소요. 테스트 142개 모두 통과, 코드 커버리지 87%. GitHub Actions 로그에서 경고나 실패 없었습니다. dev 브랜치에 merge 후 Caddy blue-green deployment로 배포 진행하겠습니다.",
        "ref_extractions": [],  # no factual issue to extract — this is normal operational message
        "ref_mcp": {
            "tldr": "CI pipeline passed with 87% coverage, proceeding to deployment",
            "intent": "report",
            "entities": {"files": [], "technologies": ["GitHub Actions", "Caddy"], "functions": [], "mentioned_users": []},
            "tags": ["ci", "deployment", "testing"],
        },
        "ref_verify": "approved",  # consistent with known CI pipeline
        "hallu_trap": False,  # should NOT create issues where none exist
    },
    # ── Case 4: 할루시네이션 트랩 (hallu_trap) ──
    {
        "id": "extract_hallu_trap",
        "title": "Extract: 할루시네이션 트랩 (OOM 아님)",
        "phase": "extract",
        "user_turn": "서버 메모리가 부족한 것 같아. OOM이 날까 걱정이야.",
        "thinking": "User is worried about OOM. Let me check the actual data. Memory shows 12Gi used out of 22Gi — that's only 55%, 10Gi free. Swap is barely touched. Load is moderate. The real issue is actually the disk. Don't assume the user's concern is correct — report what the data says.",
        "text": "서버 상태 점검 결과입니다:\n- 메모리: 22Gi 중 12Gi 사용 (55%), 10Gi 여유\n- 스왑: 8Gi 중 508Mi 사용 (6%)\n- 부하: 1min=2.55, 5min=2.46, 15min=4.02\n\nOOM 위험은 현재 낮습니다. 메모리 여유가 충분하고 스왑 사용률도 정상입니다.\n\n다만 /opt/ai_data 디스크가 92% (77G/100G)로 임계치에 근접했습니다. 모델 파일 정리가 필요합니다. 디스크 부족이 현재 더 시급한 이슈입니다.",
        "ref_extractions": [
            {"fact_type": "text", "category": "explanation",
             "evidence": "Memory: 22Gi total, 12Gi used (55%), 10Gi free — OOM risk is low"},
            {"fact_type": "text", "category": "explanation",
             "evidence": "Swap: 8Gi total, 508Mi used (6%) — normal"},
            {"fact_type": "text", "category": "requirement",
             "evidence": "/opt/ai_data disk at 92% (77G/100G) — model file cleanup needed"},
        ],
        "ref_mcp": {
            "tldr": "Server health check: OOM risk low, but /opt/ai_data disk at 92% needs cleanup",
            "intent": "report",
            "entities": {"files": ["/opt/ai_data"], "technologies": [], "functions": [], "mentioned_users": []},
            "tags": ["monitoring", "disk-space", "infrastructure"],
        },
        "ref_verify": "approved",  # factual — consistent with known 100G disk
        "hallu_trap": True,  # expected: OOM 언급 시 감점, 디스크 이슈 포착 시 점수
        "hallu_wrong_answer": ["OOM risk is high", "memory shortage critical"],
    },
    # ── Case 5: 복합 MCP 정보가 필요한 turn ──
    {
        "id": "extract_mcp_rich",
        "title": "Extract + MCP: 인프라 컨텍스트 활용",
        "phase": "mcp",
        "user_turn": "로그인 API 느린 거 수정했어. 배포하려면 어떻게 해야 해?",
        "thinking": "The fix is ready. Need to guide deployment. We have Caddy reverse proxy and blue-green deployment capability. Zero-downtime deployment is available. PostgreSQL 16 is the database. Should mention the connection pool change needs config update.",
        "text": "auth_routes.py login()에 SQLAlchemy connection pool 적용 완료했습니다. config.py에 DB_POOL_SIZE=5, DB_MAX_OVERFLOW=10 추가했습니다. PostgreSQL 16 연결 테스트 통과. Caddy reverse proxy 통해 blue-green deployment로 무중단 배포 가능합니다.",
        "ref_extractions": [
            {"fact_type": "text", "category": "explanation",
             "evidence": "SQLAlchemy connection pool applied to auth_routes.py login()"},
            {"fact_type": "text", "category": "code",
             "evidence": "DB_POOL_SIZE=5, DB_MAX_OVERFLOW=10 in config.py"},
            {"fact_type": "text", "category": "explanation",
             "evidence": "PostgreSQL 16 connection test passed, Caddy blue-green deployment available"},
        ],
        "ref_mcp": {
            "tldr": "Login API fix deployed — connection pool + config update via blue-green deployment",
            "intent": "code_change",
            "entities": {"files": ["auth_routes.py", "config.py"], "technologies": ["Python", "PostgreSQL 16", "SQLAlchemy", "Caddy"], "functions": ["login"], "mentioned_users": []},
            "tags": ["deployment", "database", "connection-pooling", "blue-green"],
        },
        "ref_verify": "approved",
        "hallu_trap": False,
    },
    # ── Case 6: verify 단계 — conflict가 있는 fact 검증 ──
    {
        "id": "verify_contradict",
        "title": "Verify: 글로벌 컨텍스트와 충돌하는 fact 검증",
        "phase": "verify",
        "user_turn": "데이터베이스를 MongoDB로 마이그레이션했어. 이제 PostgreSQL은 제거해도 되겠지?",
        "thinking": "User says they migrated to MongoDB and wants to remove PostgreSQL. But global context says the project uses PostgreSQL 16 with pg_trgm. This contradicts the known project stack. The user might be confused or testing me. Need to flag this as inconsistent with global context.",
        "text": "MongoDB로 마이그레이션 완료했습니다. 모든 데이터 이전했고, PostgreSQL은 더 이상 사용하지 않습니다.",
        "ref_extractions": [
            {"fact_type": "text", "category": "explanation",
             "evidence": "User claims migration to MongoDB completed, all data transferred"},
            {"fact_type": "text", "category": "decision",
             "evidence": "User requests PostgreSQL removal"},
        ],
        "ref_mcp": {
            "tldr": "User claims MongoDB migration complete, requests PostgreSQL removal",
            "intent": "request",
            "entities": {"files": [], "technologies": ["MongoDB", "PostgreSQL"], "functions": [], "mentioned_users": []},
            "tags": ["database", "migration", "infrastructure"],
        },
        # Verify phase: global context says PostgreSQL 16 + pg_trgm. This claim contradicts.
        "ref_verify": "escalate",  # NOT approved — contradicts known project infrastructure
        "verify_reason": "PostgreSQL 16 is the project database per global context. Claim of MongoDB migration is unverified and contradicts known infrastructure.",
        "hallu_trap": False,
    },
]

MODELS = [
    {"name": "7B Q8",           "file": "Qwen2.5-Coder-7B-Instruct.Q8_0.gguf",   "tag": "7b",    "threads": 3, "threads_batch": 3},
    {"name": "Qwen Coder 14B",  "file": "qwen2.5-coder-14b-instruct-q4_k_m.gguf", "tag": "coder14b", "threads": 4, "threads_batch": 4},
    {"name": "Qwen 2.5 14B",    "file": "Qwen2.5-14B-Instruct-Q4_K_M.gguf",      "tag": "qwen25",   "threads": 4, "threads_batch": 4},
    {"name": "Qwen3 14B",       "file": "Qwen3-14B-Q4_K_M.gguf",                 "tag": "qwen3",    "threads": 4, "threads_batch": 4},
]

ENV_FILE = "/opt/ai_data/scripts/current-mode-pod-b.env"


# ═══════════════════════════════════════════════════════════════
# 3-Tier Faithfulness (matching production extract.py)
# ═══════════════════════════════════════════════════════════════

def _cosine_faithfulness(evidence: str, source: str) -> float:
    """Tier 1: all-MiniLM-L6-v2 cosine similarity."""
    if not evidence or not source:
        return 0.0
    emb = EMBED.encode([evidence, source])
    dot = float(emb[0] @ emb[1])
    norm_a = float((emb[0] @ emb[0]) ** 0.5)
    norm_b = float((emb[1] @ emb[1]) ** 0.5)
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _nli_judge(evidence: str, source: str) -> tuple:
    """Tier 2: MiniCheck flan-t5-large NLI. Returns (faithful: bool, prob: float)."""
    if not evidence or not source:
        return False, 0.0
    labels, probs, _, _ = NLI.score(docs=[source], claims=[evidence])
    return labels[0] == 1 and probs[0] >= MC_THRESHOLD, probs[0]


def check_faithfulness(evidence: str, source: str) -> dict:
    """3-tier: embed cos → MiniCheck NLI. Production exact replica."""
    if not evidence or not source:
        return {"faithful": False, "score": 0, "method": "empty", "nli_prob": None}

    cos = _cosine_faithfulness(evidence, source)
    score = round(cos * 100, 1)

    if cos >= COSINE_FAITHFUL:
        return {"faithful": True,  "score": score, "method": "embed", "nli_prob": None}
    if cos < COSINE_UNFAITHFUL:
        return {"faithful": False, "score": score, "method": "embed", "nli_prob": None}

    # Ambiguous → Tier 2: MiniCheck NLI
    faithful_nli, prob = _nli_judge(evidence, source)
    nli_prob = round(prob, 3)
    method = "nli_faithful" if faithful_nli else "nli_reject"
    return {"faithful": faithful_nli, "score": score, "method": method, "nli_prob": nli_prob}


# ═══════════════════════════════════════════════════════════════
# Scoring helpers
# ═══════════════════════════════════════════════════════════════

def _tokenize(text):
    cleaned = re.sub(r'[.,;:\-—/()\[\]{}\'"]+', ' ', text.lower())
    return set(cleaned.split())


def _text_match(t1, t2):
    """Jaccard >= 0.25 OR (containment >= 0.5 AND intersection >= 3)."""
    w1, w2 = _tokenize(t1), _tokenize(t2)
    if not w1 or not w2: return False
    inter = len(w1 & w2)
    jac = inter / len(w1 | w2)
    cont = inter / min(len(w1), len(w2))
    return jac >= 0.25 or (cont >= 0.5 and inter >= 3)


def _cats_compatible(c1, c2):
    """Category match: exact OR one is 'bug' (wildcard, backward compat)."""
    return c1 == c2 or 'bug' in (c1, c2)


def _hallu_match(bad_phrase, evidence):
    """Strict match for hallu trap: ALL tokens from wrong answer must be in evidence."""
    bad_tokens = _tokenize(bad_phrase)
    ev_tokens = _tokenize(evidence)
    if not bad_tokens:
        return False
    return bad_tokens.issubset(ev_tokens)


def score_extractions(model_extractions, ref_extractions):
    """10-axis compatible extraction scoring.

    Weights (aligned with rubric axes 1-4 = 45% of total):
      - Evidence recall (axis 1, 15%): 40% of extract score
      - Fact type accuracy (axis 4): 25% of extract score
      - Category accuracy (axis 4): 15% of extract score
      - Precision / no extra (axis 2, 10%): 20% of extract score
    Total: 100 (scales to 45/100 in 10-axis composite)
    """
    mn, rn = len(model_extractions), len(ref_extractions)

    if rn == 0:
        # Precision-only case: clean turn with no facts to extract
        precision_score = 100 if mn == 0 else max(0, 100 - mn * 25)
        return {
            "matched": 0, "total": 0,
            "score": precision_score,
            "evidence_only": 0,
            "typed_matched": 0,
            "category_matched": 0,
            "detail": f"precision_only: {mn} extractions (0 expected)"
        }

    evidence_matched = 0   # text-matched (weakest)
    typed_matched = 0      # text + fact_type matched
    category_matched = 0   # text + fact_type + category matched (strictest)

    for ref in ref_extractions:
        for mdl in model_extractions:
            if _text_match(ref["evidence"], mdl.get("evidence", "")):
                evidence_matched += 1
                if ref.get("fact_type") == mdl.get("fact_type"):
                    typed_matched += 1
                    if _cats_compatible(ref.get("category", ""), mdl.get("category", "")):
                        category_matched += 1
                break

    ev_recall = evidence_matched / rn
    ft_acc = typed_matched / rn
    cat_acc = category_matched / rn
    extra = max(0, mn - rn)

    # Weighted score
    score = round(
        ev_recall * 40 +           # Axis 1: evidence recall
        ft_acc * 25 +              # Axis 4: fact_type accuracy
        cat_acc * 15 +             # Axis 4: category accuracy
        max(0, 20 - extra * 4)     # Axis 2: precision (no invented facts)
    )

    return {
        "matched": evidence_matched,
        "total": rn,
        "score": score,
        "evidence_only": evidence_matched,
        "typed_matched": typed_matched,
        "category_matched": category_matched,
        "detail": f"ev={evidence_matched}/{rn} ft={typed_matched}/{rn} cat={category_matched}/{rn} extra={extra}"
    }


def score_faithfulness(faithful, unfaithful, ambiguous, total_facts):
    """Score NLI faithfulness (axis 8, 10%).

    Scale aligned with rubric:
      100: >= 80% faithful, embed tier resolves most
       80: >= 70% faithful
       60: >= 50% faithful
       40: < 50% faithful
       20: < 25% faithful
        0: no facts or all unfaithful
    """
    if total_facts == 0:
        return 100, "no_facts"
    f_ratio = faithful / total_facts
    if f_ratio >= 0.80:
        score = 100
    elif f_ratio >= 0.70:
        score = 80
    elif f_ratio >= 0.50:
        score = 60
    elif f_ratio >= 0.25:
        score = 40
    else:
        score = 20
    detail = f"F={faithful}/{total_facts}({round(f_ratio*100)}%) U={unfaithful} A={ambiguous}"
    return score, detail


def score_mcp(model_mcp, ref_mcp):
    """MCP metadata scoring: tldr relevance, intent match, entities overlap, tags overlap."""
    score = 100
    detail = []

    # tldr similarity
    if _text_match(ref_mcp.get("tldr", ""), model_mcp.get("tldr", "")):
        detail.append("tldr:match")
    else:
        score -= 20
        detail.append("tldr:mismatch")

    # intent exact
    if ref_mcp.get("intent") == model_mcp.get("intent"):
        detail.append("intent:match")
    else:
        score -= 15
        detail.append(f"intent:mismatch(got={model_mcp.get('intent')})")

    # entities overlap
    ref_ents = ref_mcp.get("entities", {})
    mdl_ents = model_mcp.get("entities", {})
    for key in ("files", "technologies", "functions"):
        ref_set = set(ref_ents.get(key, []))
        mdl_set = set(mdl_ents.get(key, []))
        if ref_set:
            overlap = len(ref_set & mdl_set) / len(ref_set)
            if overlap >= 0.5:
                detail.append(f"entities.{key}:{round(overlap*100)}%")
            else:
                score -= 10
                detail.append(f"entities.{key}:low({round(overlap*100)}%)")
        elif mdl_set:
            score -= 5
            detail.append(f"entities.{key}:unexpected")

    # tags count (2-5 required by rubric)
    mdl_tags = set(model_mcp.get("tags", []))
    if len(mdl_tags) < 2:
        score -= 10
        detail.append(f"tags:too_few({len(mdl_tags)})")
    elif len(mdl_tags) > 5:
        score -= 5
        detail.append(f"tags:too_many({len(mdl_tags)})")

    # tags overlap
    ref_tags = set(ref_mcp.get("tags", []))
    if ref_tags and mdl_tags:
        overlap = len(ref_tags & mdl_tags) / len(ref_tags | mdl_tags)
        if overlap >= 0.3:
            detail.append(f"tags:ok({round(overlap*100)}%)")
        else:
            score -= 10
            detail.append(f"tags:low({round(overlap*100)}%)")

    return max(0, score), "; ".join(detail)


def score_verify(model_verdict, ref_verdict, reasoning="", ref_reason=""):
    """Verify verdict scoring with reasoning quality check."""
    score = 100
    detail = []

    if model_verdict == ref_verdict:
        detail.append("verdict:match")
    elif {model_verdict, ref_verdict} == {"escalate", "rejected"}:
        score -= 30
        detail.append("verdict:partial(escalate vs reject)")
    elif {model_verdict, ref_verdict} == {"approved", "escalate"}:
        score -= 60
        detail.append("verdict:weak(should have escalated)")
    elif {model_verdict, ref_verdict} == {"approved", "rejected"}:
        score -= 80
        detail.append("verdict:mismatch")
    else:
        score -= 50
        detail.append(f"verdict:unknown(model={model_verdict})")

    # Reasoning quality: check if reference reasoning keywords appear
    if ref_reason and reasoning:
        ref_kw = set(re.sub(r'[^a-z0-9 ]', ' ', ref_reason.lower()).split())
        mdl_kw = set(re.sub(r'[^a-z0-9 ]', ' ', reasoning.lower()).split())
        kw_hit = len(ref_kw & mdl_kw)
        if kw_hit >= 3:
            detail.append(f"reasoning:good({kw_hit}kw)")
        elif kw_hit >= 1:
            score -= 10
            detail.append(f"reasoning:weak({kw_hit}kw)")
        else:
            score -= 20
            detail.append("reasoning:empty_or_off")

    return max(0, score), "; ".join(detail)


# ═══════════════════════════════════════════════════════════════
# 10-Axis Scoring (Pro-designed — checklist.yaml)
# Each axis returns 0-10. Composite = Σ(axis * weight_factor).
# Weights: 15+10+10+10+10+5+10+10+5+5 = 100
# ═══════════════════════════════════════════════════════════════

AXIS_WEIGHTS = {
    "ax1_recall": 1.5, "ax2_precision": 1.0, "ax3_hallu": 1.0,
    "ax4_fact_type": 1.0, "ax5_mcp": 1.0, "ax6_mcp_context": 0.5,
    "ax7_verify": 1.0, "ax8_nli": 1.0, "ax9_json": 0.5,
    "ax10_consistency": 0.5,
}


def _scale_to_10(ratio, thresholds=None):
    """Map a ratio [0,1] to 0-10 using step thresholds.
    Default: 10:>=0.95, 8:>=0.80, 6:>=0.50, 4:>=0.25, 2:>0, 0:0.
    """
    if thresholds is None:
        thresholds = [(0.95, 10), (0.80, 8), (0.50, 6), (0.25, 4), (0.001, 2), (-1, 0)]
    for t, s in thresholds:
        if ratio >= t:
            return s
    return 0


def score_axis1_recall(cases):
    """Axis 1: Fact Recall (weight 15). Evidence matched / total reference."""
    total_ref = sum(c["extract"].get("ref_n", 0) for c in cases)
    total_matched = sum(c["extract"].get("evidence_matched", 0) for c in cases)
    if total_ref == 0:
        return 10  # all cases expect 0 facts — perfect
    ratio = total_matched / total_ref
    return _scale_to_10(ratio)


def score_axis2_precision(cases):
    """Axis 2: Fact Precision (weight 10). False positive rate, extract_clean critical."""
    clean_case = next((c for c in cases if c["case_id"] == "extract_clean"), None)
    clean_facts = clean_case["extract"]["n_facts"] if clean_case else 0

    total_extra = 0
    extra_unfaithful = 0
    for c in cases:
        extra = max(0, c["extract"]["n_facts"] - c["extract"].get("ref_n", 0))
        total_extra += extra
        if extra > 0:
            extra_unfaithful += c["extract"].get("unfaithful", 0)

    if clean_facts == 0 and total_extra <= 2 and extra_unfaithful == 0:
        return 10
    elif clean_facts == 0 and total_extra <= 3 and extra_unfaithful <= 1:
        return 8
    elif clean_facts <= 1 and total_extra <= 5:
        return 6
    elif clean_facts <= 2 and total_extra <= 8:
        return 4
    elif total_extra > 8:
        return 2
    else:
        return 0


def score_axis3_hallu(cases):
    """Axis 3: Hallucination Resistance (weight 10). hallu_trap case."""
    ht = next((c for c in cases if c["case_id"] == "extract_hallu_trap"), None)
    if not ht:
        return 10
    hallu_hit = ht.get("hallu_trap_hit", False)
    # Check disk capture from extract detail (evidence_matched > 0 for disk-related ref facts)
    # The ref has 3 facts: mem-low-risk, swap-normal, disk-92%
    # evidence_matched >= 2 and hallu_trap_hit=False means disk issue captured without OOM
    ev_matched = ht["extract"].get("evidence_matched", 0)

    if ev_matched >= 2 and not hallu_hit:
        return 10  # disk issue captured, no OOM hallucination
    elif ev_matched >= 1 and not hallu_hit:
        return 8   # some facts captured, no OOM
    elif ev_matched >= 1 and hallu_hit:
        return 6   # disk captured but OOM also mentioned
    elif ev_matched == 0 and hallu_hit:
        return 4   # OOM as primary, disk missed
    elif ev_matched == 0 and not hallu_hit:
        return 2   # missed everything
    else:
        return 0


def score_axis4_fact_type(cases):
    """Axis 4: Fact Type Accuracy (weight 10). typed_matched + category_matched accuracy."""
    total_ref = sum(c["extract"].get("ref_n", 0) for c in cases)
    total_typed = sum(c["extract"].get("typed_matched", 0) for c in cases)
    total_cat = sum(c["extract"].get("category_matched", 0) for c in cases)

    if total_ref == 0:
        return 10
    typ_ratio = total_typed / total_ref
    cat_ratio = total_cat / total_ref
    avg_ratio = typ_ratio * 0.6 + cat_ratio * 0.4
    return _scale_to_10(avg_ratio)


def score_axis5_mcp(cases):
    """Axis 5: MCP Quality (weight 10). Average MCP score / 10."""
    mcp_scores = [c["mcp"]["score"] for c in cases
                  if c["mcp"].get("score", 0) > 0 and c.get("phase") != "verify"]
    if not mcp_scores:
        return 0
    avg = sum(mcp_scores) / len(mcp_scores)
    return round(min(10, max(0, avg / 10)))


def score_axis6_mcp_context(cases):
    """Axis 6: MCP Context Awareness (weight 5). extract_mcp_rich case."""
    mcp_rich = next((c for c in cases if c["case_id"] == "extract_mcp_rich"), None)
    if not mcp_rich:
        return 10
    entities = mcp_rich.get("mcp", {}).get("parsed", {}).get("entities", {})
    technologies = [t.lower() for t in entities.get("technologies", [])]
    files = entities.get("files", [])

    score = 0
    # PostgreSQL 16 check
    if any("postgres" in t for t in technologies):
        score += 3
    # Caddy check
    if any("caddy" in t for t in technologies):
        score += 3
    # config.py check
    if any("config.py" in f.lower() for f in files):
        score += 4
    # Scale to 0-10
    return min(10, score)


def score_axis7_verify(cases):
    """Axis 7: Verify Accuracy (weight 10). verify_contradict case."""
    vc = next((c for c in cases if c["case_id"] == "verify_contradict"), None)
    if not vc:
        return 10
    verdict = vc.get("verify", {}).get("parsed", {}).get("final_verdict", "")
    reasoning = vc.get("verify", {}).get("parsed", {}).get("reasoning", "")
    vscore = vc.get("verify", {}).get("score", 0)

    if verdict == "escalate" and vscore >= 80:
        return 10  # escalate + good reasoning
    elif verdict == "escalate":
        return 8   # escalate + weak reasoning
    elif verdict == "rejected":
        return 6   # correct direction but not escalate
    elif verdict == "approved":
        return 4   # missed contradiction
    elif verdict:
        return 2   # unrelated verdict
    else:
        return 0   # parse fail


def score_axis8_nli(cases):
    """Axis 8: 3-Tier NLI Faithfulness (weight 10). Faithful ratio across all facts."""
    total_facts = sum(c["extract"]["n_facts"] for c in cases)
    total_faithful = sum(c["extract"]["faithful"] for c in cases)
    if total_facts == 0:
        return 10
    ratio = total_faithful / total_facts
    return _scale_to_10(ratio, [(0.80, 10), (0.70, 8), (0.50, 6), (0.25, 4), (0.001, 2), (-1, 0)])


def score_axis9_json(cases):
    """Axis 9: JSON Stability (weight 5). Parse success rate."""
    total = len(cases)
    ok = sum(1 for c in cases if c["extract"].get("json_ok", False))
    if total == 0:
        return 0
    ratio = ok / total
    return _scale_to_10(ratio, [(1.0, 10), (0.80, 8), (0.60, 6), (0.40, 4), (0.20, 2), (-1, 0)])


def score_axis10_consistency(cases):
    """Axis 10: Output Consistency (weight 5). Std dev of extract scores."""
    scores = [c["extract"]["ref_score"] for c in cases if "extract" in c]
    if len(scores) <= 1:
        return 10
    mean = sum(scores) / len(scores)
    var = (sum((s - mean) ** 2 for s in scores) / len(scores)) ** 0.5
    # Lower variance = higher consistency
    if var <= 5: return 10
    elif var <= 10: return 8
    elif var <= 15: return 6
    elif var <= 20: return 4
    elif var <= 25: return 2
    else: return 0


def compute_10axis(cases):
    """Compute all 10 axis scores from collected case results.
    Returns dict of axis_name → score (0-10) and weighted composite (0-100).
    """
    axes = {
        "ax1_recall": score_axis1_recall(cases),
        "ax2_precision": score_axis2_precision(cases),
        "ax3_hallu": score_axis3_hallu(cases),
        "ax4_fact_type": score_axis4_fact_type(cases),
        "ax5_mcp": score_axis5_mcp(cases),
        "ax6_mcp_context": score_axis6_mcp_context(cases),
        "ax7_verify": score_axis7_verify(cases),
        "ax8_nli": score_axis8_nli(cases),
        "ax9_json": score_axis9_json(cases),
        "ax10_consistency": score_axis10_consistency(cases),
    }
    composite = round(sum(s * AXIS_WEIGHTS[k] for k, s in axes.items()))
    axes["composite"] = composite
    return axes


def grade_10axis(composite):
    """Grade based on checklist.yaml grading_scale."""
    if composite >= 90: return "S"
    elif composite >= 75: return "A"
    elif composite >= 60: return "B"
    elif composite >= 40: return "C"
    elif composite >= 20: return "D"
    else: return "F"


# ═══════════════════════════════════════════════════════════════
# Pod B swap
# ═══════════════════════════════════════════════════════════════

def swap_model(model_file, model_name, threads=4, threads_batch=4):
    log(f"  Swapping to {model_name} ({model_file}) [t={threads}/{threads_batch}]...")
    with open(ENV_FILE, "w") as f:
        f.write(
            f"MODE=day\n"
            f"MODEL_NAME=\"{model_name}\"\n"
            f"PORT=8082\n"
            f"MODEL_FILE={model_file}\n"
            f"CTX_SIZE=16384\n"
            f"THREADS={threads}\n"
            f"THREADS_BATCH={threads_batch}\n"
            f"CACHE_RAM=2048\n"
            f"EVICT_ROOM=10000\n"
            f"MEMORY_CHECK=14000\n"
            f"MEMORY_CHECK_MODE=warn\n"
            f"REPORT_MEMORY=1\n"
            f"MLOCK=0\n"
        )
    subprocess.run(["systemctl", "--user", "stop", "container-devforge-pod-b"],
                   capture_output=True, timeout=60)
    time.sleep(3)
    subprocess.run(["systemctl", "--user", "reset-failed", "container-devforge-pod-b"],
                   capture_output=True, timeout=10)

    # Extend HealthStartPeriod via drop-in (14B loading may exceed default 300s)
    dropin_dir = os.path.expanduser("~/.config/systemd/user/container-devforge-pod-b.service.d")
    dropin_file = os.path.join(dropin_dir, "50-health-override.conf")
    os.makedirs(dropin_dir, exist_ok=True)
    with open(dropin_file, "w") as f:
        f.write("[Service]\nHealthStartPeriod=600s\n")
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=10)

    t0 = time.monotonic()
    result = subprocess.run(["systemctl", "--user", "start", "container-devforge-pod-b"],
                            capture_output=True, timeout=60, text=True)
    if result.returncode != 0:
        # Clean up drop-in on failure
        os.remove(dropin_file)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=10)
        raise RuntimeError(f"systemctl start failed: {result.stderr.strip()[:300]}")

    # Wait for health + clean up drop-in
    try:
        for i in range(300):
            try:
                r = urllib.request.urlopen(urllib.request.Request(
                    f"http://127.0.0.1:{MODEL_REGISTRY['extractor']['port']}/health"), timeout=5)
                if r.status == 200:
                    load_s = round(time.monotonic() - t0, 1)
                    log(f"  Ready in {i+1}s ({load_s}s total)")
                    time.sleep(3)
                    return load_s
            except Exception:
                pass
            if i % 30 == 0 and i > 0:
                log(f"  ... {i+1}s elapsed")
            time.sleep(2)
        raise RuntimeError(f"Failed to start {model_name}")
    finally:
        # Always restore original HealthStartPeriod
        if os.path.exists(dropin_file):
            os.remove(dropin_file)
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=10)


def _extract_tokens(usage):
    if not isinstance(usage, dict): return 0
    for key in ("completion_tokens", "tokens_predicted", "predicted_n"):
        v = usage.get(key)
        if isinstance(v, (int, float)) and v > 0: return int(v)
    timings = usage.get("timings", {})
    if isinstance(timings, dict):
        for key in ("predicted_n", "predicted_tokens"):
            v = timings.get(key)
            if isinstance(v, (int, float)) and v > 0: return int(v)
    return 0


def call_reviewer(system, user, timeout=600):
    t0 = time.monotonic()
    try:
        meta = call_llm(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            model="extractor", max_tokens=1024, temperature=0.1,
            timeout=timeout, json_mode=True, return_meta=True)
        raw = meta.get("content", "")
        parsed = parse_llm_json(raw)
        elapsed = round(time.monotonic() - t0, 1)
        tok = _extract_tokens(meta.get("usage", {}))
        tps = round(tok / elapsed, 2) if elapsed > 0 and tok > 0 else 0
        return {"ok": parsed is not None, "parsed": parsed, "raw": raw[:500],
                "elapsed_s": elapsed, "tokens": tok, "tps": tps, "error": ""}
    except Exception as e:
        elapsed = round(time.monotonic() - t0, 1)
        return {"ok": False, "parsed": None, "raw": "", "elapsed_s": elapsed,
                "tokens": 0, "tps": 0, "error": str(e)[:200]}


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def main():
    TEST = test_setup("model_comparison", "14B Q4_K_M x3 비교: Extract + NLI + MCP + Verify 4-phase")
    all_results = {}

    # Pre-check
    log("Pre-check: Pod B state...")
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    port_used = False
    try:
        s.connect(("127.0.0.1", 8082)); port_used = True; s.close()
    except Exception:
        s.close()
    result = subprocess.run(["systemctl", "--user", "is-active", "container-devforge-pod-b"],
                            capture_output=True, text=True)
    svc_state = result.stdout.strip()
    if port_used:
        log(f"  :8082 in use, svc={svc_state} — stopping")
        subprocess.run(["systemctl", "--user", "stop", "container-devforge-pod-b"], capture_output=True, timeout=60)
        time.sleep(3)
        subprocess.run(["systemctl", "--user", "reset-failed", "container-devforge-pod-b"], capture_output=True, timeout=10)
    elif svc_state in ("failed", "activating"):
        log(f"  svc={svc_state} — resetting")
        subprocess.run(["systemctl", "--user", "reset-failed", "container-devforge-pod-b"], capture_output=True, timeout=10)
    else:
        log(f"  svc={svc_state}, :8082 free — ready")

    for mi, model in enumerate(MODELS):
        tag = model["tag"]
        log(f"\n{'='*60}")
        log(f"  [{mi+1}/{len(MODELS)}] {model['name']} ({model['file']})")
        log(f"{'='*60}")

        try:
            load_s = swap_model(model["file"], model["name"],
                                model.get("threads", 4), model.get("threads_batch", 4))
        except Exception as e:
            log(f"  !! SWAP FAILED: {str(e)[:300]} — skipping")
            all_results[tag] = {"model": model, "error": f"swap_failed: {str(e)[:300]}"}
            continue

        # Warm-up
        log("  ── Warm-up (2 calls) ──")
        w1 = call_reviewer(SYSTEM_DAY_EXTRACT, "user_turn: hello\nthinking: greeting\n\n=== text ===\nHello! How can I help?", timeout=120)
        w2 = call_reviewer(SYSTEM_DAY_EXTRACT, "user_turn: hello\nthinking: greeting\n\n=== text ===\nHello! How can I help?", timeout=120)
        warmup_ok = bool(w1["parsed"]) or bool(w2["parsed"])
        log(f"  W#1: {w1['elapsed_s']}s/{w1['tps']}t/s ok={w1['ok']}  W#2: {w2['elapsed_s']}s/{w2['tps']}t/s ok={w2['ok']}  json_valid={warmup_ok}")

        model_data = {
            "model": model, "load_s": load_s,
            "warmup": {"call1_elapsed": w1["elapsed_s"], "call2_elapsed": w2["elapsed_s"],
                       "call1_tps": w1["tps"], "call2_tps": w2["tps"], "json_mode_ok": warmup_ok},
            "cases": [],
        }

        for ci, case in enumerate(GT_CASES):
            phase = case["phase"]
            log(f"  ── [{ci+1}/6] {case['title']} [{phase}] ──")

            # Initialize all phase variables (verify-only case needs these)
            extractions, faithful, unfaithful, ambiguous = [], 0, 0, 0
            ext_score = {"score": 0, "matched": 0, "total": 0, "detail": ""}
            mcp_score, mcp_det, mcp_parsed = 0, "", {}
            hallu_hit = False
            resp = {"elapsed_s": 0, "tps": 0, "tokens": 0, "error": ""}
            turn = ""

            # ── Phase A/B: Extract + Faithfulness (all cases) ──
            if phase in ("extract", "mcp", "verify"):
                turn = f"=== user_turn ===\n{case['user_turn']}\n\n=== thinking ===\n{case['thinking']}\n\n=== text ===\n{case['text']}"
                sys_extract = SYSTEM_DAY_EXTRACT + ("\n\n" + RUBRIC_FULL if USE_RUBRIC else "")
                resp = call_reviewer(sys_extract, turn, timeout=600)
                extractions = []
                if resp["parsed"] and isinstance(resp["parsed"], dict):
                    extractions = resp["parsed"].get("extractions", [])
                if not isinstance(extractions, list):
                    extractions = []

                # 3-tier faithfulness
                sources = {"user": case["user_turn"], "thinking": case["thinking"], "text": case["text"]}
                faithful, unfaithful, ambiguous = 0, 0, 0
                nli_results = []
                for ex in extractions:
                    ft = ex.get("fact_type", "")
                    ev = ex.get("evidence", "")
                    v = check_faithfulness(ev, sources.get(ft, ""))
                    nli_results.append(v)
                    ex["_faithful"] = v["faithful"]
                    ex["_faithful_score"] = v["score"]
                    ex["_faithful_method"] = v["method"]
                    if v["faithful"]:
                        faithful += 1
                    elif v["method"].startswith("nli"):
                        ambiguous += 1
                    else:
                        unfaithful += 1

                ext_score = score_extractions(extractions, case["ref_extractions"])
                faith_score, faith_det = score_faithfulness(faithful, unfaithful, ambiguous, len(extractions))
                json_ok = resp["ok"]
                log(f"  Extract: {len(extractions)} facts ({faithful}F/{unfaithful}U/{ambiguous}A)"
                    f" | ref_match={ext_score['matched']}/{ext_score['total']} (ev={ext_score.get('evidence_only','?')} cat={ext_score.get('category_matched','?')})"
                    f" | score={ext_score['score']}/100 faith={faith_score}/100 json={'OK' if json_ok else 'FAIL'}")

                # ── Incremental save: extract phase done ──
                partial_case = {
                    "case_id": case["id"], "phase": phase,
                    "extract": {"n_facts": len(extractions), "faithful": faithful,
                                "unfaithful": unfaithful, "ambiguous": ambiguous,
                                "ref_n": len(case["ref_extractions"]),
                                "ref_score": ext_score["score"], "ref_detail": ext_score["detail"],
                                "evidence_matched": ext_score.get("evidence_only", 0),
                                "typed_matched": ext_score.get("typed_matched", 0),
                                "category_matched": ext_score.get("category_matched", 0),
                                "faithfulness_score": faith_score,
                                "json_ok": json_ok},
                    "mcp": {}, "verify": {}, "hallu_trap_hit": False,
                    "speed": {"elapsed_s": resp["elapsed_s"], "tps": resp["tps"], "tokens": resp["tokens"]},
                    "error": resp.get("error", ""),
                }
                model_data["cases"].append(partial_case)
                all_results[tag] = dict(model_data)
                with open(OUTPUT, "w") as f:
                    json.dump(all_results, f, ensure_ascii=False, indent=2)

                # ── Phase C: MCP metadata (skip for verify-only cases) ──
                mcp_score = 0
                mcp_det = ""
                mcp_parsed = {}
                if phase in ("mcp", "extract"):
                    mcp_prompt = f"=== Original Turn ===\n{turn}\n\n=== Extracted Facts ===\n{json.dumps(extractions[:5], ensure_ascii=False, indent=2)}"
                    sys_mcp = SYSTEM_DAY_MCP + ("\n\n" + RUBRIC_FULL if USE_RUBRIC else "")
                    mcp_resp = call_reviewer(sys_mcp, mcp_prompt, timeout=600)
                    if mcp_resp["parsed"] and isinstance(mcp_resp["parsed"], dict):
                        mcp_parsed = mcp_resp["parsed"]
                        mcp_score, mcp_det = score_mcp(mcp_parsed, case["ref_mcp"])
                    log(f"  MCP: score={mcp_score}/100 ({mcp_det})")

                # ── Incremental save: MCP phase done ──
                if model_data["cases"]:
                    model_data["cases"][-1]["mcp"] = {"score": mcp_score, "detail": mcp_det, "parsed": mcp_parsed}
                    model_data["cases"][-1]["hallu_trap_hit"] = hallu_hit
                    all_results[tag] = dict(model_data)
                    with open(OUTPUT, "w") as f:
                        json.dump(all_results, f, ensure_ascii=False, indent=2)

                # ── hallu trap check ──
                hallu_hit = False
                if case.get("hallu_trap"):
                    for ex in extractions:
                        ev = ex.get("evidence", "").lower()
                        for bad in case.get("hallu_wrong_answer", []):
                            if _hallu_match(bad, ev):
                                hallu_hit = True
                                ex["_hallu_trap_hit"] = True
                                break

            # ── Phase D: Verify ──
            verify_score = 0
            verify_det = ""
            verify_parsed = {}
            if phase == "verify":
                vfy_prompt = (
                    f"{GLOBAL_CONTEXT}\n\n"
                    f"=== TURN ===\n{turn}\n\n"
                    f"=== EXTRACTED FACTS ===\n{json.dumps(extractions[:5], ensure_ascii=False, indent=2)}\n\n"
                    f"Verify whether these facts are consistent with the global project context above."
                )
                sys_verify = SYSTEM_VERIFY + ("\n\n" + RUBRIC_FULL if USE_RUBRIC else "")
                vfy_resp = call_reviewer(sys_verify, vfy_prompt, timeout=600)
                if vfy_resp["parsed"] and isinstance(vfy_resp["parsed"], dict):
                    verify_parsed = vfy_resp["parsed"]
                    model_verdict = verify_parsed.get("final_verdict", "unknown")
                    verify_score, verify_det = score_verify(model_verdict, case["ref_verify"],
                                                            verify_parsed.get("reasoning", ""),
                                                            case.get("verify_reason", ""))
                log(f"  Verify: verdict={verify_parsed.get('final_verdict','?')} "
                    f"score={verify_score}/100 ({verify_det})")
            else:
                verify_score = 100  # extract cases — verify not tested
                verify_det = "not_applicable"

            # ── Assemble final case result (replaces partial saved after extract/MCP) ──
            case_result = {
                "case_id": case["id"],
                "phase": phase,
                "extract": {
                    "n_facts": len(extractions),
                    "faithful": faithful,
                    "unfaithful": unfaithful,
                    "ambiguous": ambiguous,
                    "ref_n": len(case["ref_extractions"]),
                    "ref_score": ext_score["score"],
                    "ref_detail": ext_score["detail"],
                    "evidence_matched": ext_score.get("evidence_only", 0),
                    "typed_matched": ext_score.get("typed_matched", 0),
                    "category_matched": ext_score.get("category_matched", 0),
                    "faithfulness_score": faith_score,
                    "json_ok": json_ok,
                },
                "mcp": {"score": mcp_score, "detail": mcp_det, "parsed": mcp_parsed},
                "verify": {"score": verify_score, "detail": verify_det,
                           "parsed": verify_parsed if phase == "verify" else {}},
                "hallu_trap_hit": hallu_hit,
                "speed": {"elapsed_s": resp["elapsed_s"], "tps": resp["tps"], "tokens": resp["tokens"]},
                "error": resp.get("error", ""),
            }
            if model_data["cases"] and model_data["cases"][-1].get("case_id") == case["id"]:
                model_data["cases"][-1] = case_result
            else:
                model_data["cases"].append(case_result)

            # Incremental save
            with open(OUTPUT, "w") as f:
                json.dump(all_results, f, ensure_ascii=False, indent=2)

        all_results[tag] = model_data
        log(f"  Saved to {OUTPUT} ({tag} done)")

    # ── Final Report (10-Axis) ──
    test_label = "TEST 2 (with rubric)" if USE_RUBRIC else "TEST 1 (no rubric)"
    log(f"\n{'='*60}")
    log(f"  LLM EXTRACT COMPARISON — FINAL REPORT — {test_label}")
    log(f"  Scoring: 10-Axis (checklist.yaml) — each axis 0-10, weighted composite 0-100")
    log(f"{'='*60}")

    all_axis_results = {}
    for tag, data in all_results.items():
        if "error" in data:
            log(f"\n  [{data['model']['name']}] ERROR: {data['error']}")
            all_axis_results[tag] = {"error": data["error"]}
            continue
        m = data["model"]
        cases = data["cases"]
        n = len(cases)
        axes = compute_10axis(cases)
        grade = grade_10axis(axes["composite"])
        all_axis_results[tag] = axes

        # Speed stats
        avg_s = sum(c["speed"]["elapsed_s"] for c in cases) / n if n > 0 else 0
        speeds_tps = [c["speed"]["tps"] for c in cases if c["speed"]["tps"] > 0]
        avg_tps = sum(speeds_tps) / len(speeds_tps) if speeds_tps else 0
        total_f = sum(c["extract"]["faithful"] for c in cases)
        total_u = sum(c["extract"]["unfaithful"] for c in cases)
        total_a = sum(c["extract"]["ambiguous"] for c in cases)
        total_ev = sum(c["extract"].get("evidence_matched", 0) for c in cases)
        total_typed = sum(c["extract"].get("typed_matched", 0) for c in cases)
        json_oks = sum(1 for c in cases if c["extract"].get("json_ok"))

        log(f"\n  [{m['name']}] ({m['file']})")
        log(f"    Load: {data['load_s']}s | Warm-up: #{1} {data['warmup']['call1_elapsed']}s/{data['warmup']['call1_tps']}t/s, #{2} {data['warmup']['call2_elapsed']}s/{data['warmup']['call2_tps']}t/s")
        log(f"    Faith: {total_f}F/{total_u}U/{total_a}A | Evidence: {total_ev} | Type: {total_typed} | JSON: {json_oks}/{n} | Speed: {avg_s:.0f}s/case, {avg_tps:.1f}t/s")
        log(f"    ── 10-Axis Scores (0-10) ──")
        log(f"    축1 Recall: {axes['ax1_recall']:>2}  축2 Precision: {axes['ax2_precision']:>2}  축3 Hallu: {axes['ax3_hallu']:>2}  축4 FactType: {axes['ax4_fact_type']:>2}")
        log(f"    축5 MCP:     {axes['ax5_mcp']:>2}  축6 MCP Ctx:   {axes['ax6_mcp_context']:>2}  축7 Verify: {axes['ax7_verify']:>2}  축8 NLI:    {axes['ax8_nli']:>2}")
        log(f"    축9 JSON:    {axes['ax9_json']:>2}  축10 Consist:  {axes['ax10_consistency']:>2}")
        log(f"    ★ COMPOSITE: {axes['composite']}/100 [{grade}] ★")

    # ── 10-Axis Comparison Table ──
    log(f"\n  ── 10-Axis Comparison ──")
    tags = [m["tag"] for m in MODELS]
    ax_labels = ["축1", "축2", "축3", "축4", "축5", "축6", "축7", "축8", "축9", "축10", "COMP", "G"]
    ax_keys = ["ax1_recall", "ax2_precision", "ax3_hallu", "ax4_fact_type",
               "ax5_mcp", "ax6_mcp_context", "ax7_verify", "ax8_nli",
               "ax9_json", "ax10_consistency"]
    header = f"  {'Model':20s}" + "".join(f'{a:>6s}' for a in ax_labels)
    log(header); log(f"  {'─'* (22 + 6 * len(ax_labels))}")
    for mi, model in enumerate(MODELS):
        tag = model["tag"]
        parts = [f"  {model['name']:20s}"]
        ax = all_axis_results.get(tag, {})
        if "error" in ax:
            parts.append(" ERR")
        else:
            for k in ax_keys:
                parts.append(f"{ax.get(k,0):>6d}")
            parts.append(f"{ax.get('composite',0):>6d}")
            parts.append(f"{grade_10axis(ax.get('composite',0)):>6s}")
        log("".join(parts))

    # Case detail table
    log(f"\n  ── Extract Evidence Matched by Case ──")
    col_w = max(10, *[len(t) + 2 for t in tags])
    header = f"  {'Case':20s} {'Phase':8s}" + "".join(f'{t:>{col_w}}' for t in tags)
    log(header); log(f"  {'─'* (30 + col_w * len(tags))}")
    for ci, case in enumerate(GT_CASES):
        cid = case["id"][:20]
        ph = case["phase"]
        parts = [f"  {cid:20s} {ph:8s}"]
        for t in tags:
            d = all_results.get(t, {})
            c_list = d.get("cases", [{}])
            if ci < len(c_list) and "error" not in d:
                ev = c_list[ci].get("extract", {}).get("evidence_matched", 0)
                rn = c_list[ci].get("extract", {}).get("ref_n", 0)
                parts.append(f"{f'{ev}/{rn}':>{col_w}}")
            else:
                parts.append(f"{'ERR':>{col_w}}")
        log("".join(parts))

    # Restore 7B
    log(f"\n  Restoring 7B extractor...")
    with open(ENV_FILE, "w") as f:
        f.write(
            "MODE=day\n"
            "MODEL_NAME=extractor\n"
            "PORT=8082\n"
            "MODEL_FILE=Qwen2.5-Coder-7B-Instruct.Q8_0.gguf\n"
            "CTX_SIZE=8192\n"
            "THREADS=2\nTHREADS_BATCH=2\n"
            "CACHE_RAM=512\n"
            "EVICT_ROOM=0\n"
            "MLOCK=0\n"
        )
    result = subprocess.run(["systemctl", "--user", "restart", "container-devforge-pod-b"],
                            capture_output=True, timeout=120, text=True)
    if result.returncode != 0:
        log(f"  !! 7B 복원 실패 (exit={result.returncode}): {result.stderr.strip()[:300]}")
        log(f"  수동 복원: systemctl --user restart container-devforge-pod-b")
    else:
        log(f"  7B extractor 복원 완료")
    log(f"{'='*60}")
    test_complete("comparison complete")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="14B Q4_K_M 5-Model Extract Comparison")
    p.add_argument("--rubric", action="store_true", help="Enable Test 2 rubric injection")
    args = p.parse_args()
    USE_RUBRIC = args.rubric
    if USE_RUBRIC:
        OUTPUT = "/tmp/14b_comparison_results_rubric.json"
    main()

"""
azure/servicebus | Service Bus ingestion for conversation data: receive messages and archive to Blob | needs:azure-servicebus,azure-storage-blob | receive_and_ingest()
"""
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

from app.db import get_db_connection, _USER_DB_DIR

logger = logging.getLogger(__name__)

_SB_CONN_STR = os.getenv("SERVICE_BUS_CONNECTION_STRING")
_BLOB_CONN_STR = os.getenv("AZURE_BLOB_CONNECTION_STRING")
_QUEUE_NAME = os.getenv("SERVICE_BUS_QUEUE_NAME", "seedling-inbox")
_BLOB_CONTAINER = os.getenv("BLOB_ARCHIVE_CONTAINER", "seedling-raw-inbox")

# ── 개념 추출 (Service Bus → Concept/ConceptRelation) ──────────────────────

_STOPWORDS: set[str] = {
    # English
    'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
    'should', 'may', 'might', 'can', 'shall', 'to', 'of', 'in', 'for',
    'on', 'with', 'at', 'by', 'from', 'as', 'into', 'through', 'during',
    'before', 'after', 'above', 'below', 'between', 'under', 'again',
    'further', 'then', 'once', 'here', 'there', 'when', 'where', 'why',
    'how', 'all', 'both', 'each', 'few', 'more', 'most', 'other', 'some',
    'such', 'no', 'nor', 'not', 'only', 'own', 'same', 'so', 'than',
    'too', 'very', 'just', 'because', 'but', 'and', 'or', 'if', 'while',
    'this', 'that', 'these', 'those', 'it', 'its', 'he', 'she', 'they',
    'them', 'we', 'you', 'i', 'me', 'my', 'your', 'his', 'her', 'our',
    'their', 'what', 'which', 'who', 'whom',
    # Korean
    '있다', '없다', '하다', '되다', '보다', '오다', '가다', '주다', '받다',
    '그', '이', '저', '것', '수', '등', '들', '및', '더', '더욱',
    '매우', '정말', '진짜', '좀', '잘', '안', '네', '아니', '응',
    '또', '또한', '그리고', '하지만', '그래서', '그러나', '그런데',
    '이런', '그런', '저런', '어떤', '무슨', '어느', '여기', '거기',
    '저기', '지금', '현재', '지난', '다음', '오늘', '내일', '어제',
}

_EN_PHRASE_RE = re.compile(r'[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+')
_KO_COMPOUND_RE = re.compile(r'[가-힣]{3,}')
_EN_WORD_RE = re.compile(r'\b[a-zA-Z]{4,}\b')


def _extract_terms(text: str, max_terms: int = 10) -> list[str]:
    """간단한 용어 추출 (한국어/영어). Service Bus 메시지에서 개념 후보 발굴"""
    terms: list[str] = []
    # 영문 복합어 (Azure Service Bus → "Azure Service Bus")
    terms.extend(p.strip() for p in _EN_PHRASE_RE.findall(text) if len(p.strip()) > 4)
    # 한국어 복합어 (3글자 이상)
    terms.extend(c for c in _KO_COMPOUND_RE.findall(text) if c not in _STOPWORDS)
    # 영문 유의미 단어 (4글자 이상)
    terms.extend(w.lower() for w in _EN_WORD_RE.findall(text) if w.lower() not in _STOPWORDS)

    seen: set[str] = set()
    result: list[str] = []
    for t in terms:
        key = t.lower()
        if key not in seen and key not in _STOPWORDS:
            seen.add(key)
            result.append(t)
    return result[:max_terms]


def _ingest_concepts_to_graph(content: str, source: str):
    """Service Bus 메시지에서 용어를 추출해 Concept + ConceptRelation 기록"""
    terms = _extract_terms(content)
    if not terms:
        return
    try:
        from app.concept_mapper import find_or_create_concept, add_relation_if_high_confidence

        for term in terms:
            find_or_create_concept(term, source="servicebus")

        source_concept = f"source:{source}"
        for term in terms:
            add_relation_if_high_confidence(
                source_concept, term, "generates", 0.6, source="servicebus",
            )
    except Exception:
        pass  # 개념 추출은 best-effort


async def receive_and_ingest(max_count: int = 32, max_wait: int = 10) -> dict:
    """
    Receive messages from Service Bus queue, ingest to per-user DB,
    archive to Blob. Returns processing stats.

    On DB lock -> abandon (message stays in queue for next cycle).
    """
    if not _SB_CONN_STR:
        return {"status": "no_connection_string", "processed": 0}

    from azure.servicebus import ServiceBusClient

    stats = {"received": 0, "ingested": 0, "abandoned": 0, "errors": 0}

    try:
        client = ServiceBusClient.from_connection_string(_SB_CONN_STR)
        receiver = client.get_queue_receiver(queue_name=_QUEUE_NAME)

        messages = receiver.receive_messages(
            max_message_count=max_count, max_wait_time=max_wait
        )

        for msg in messages:
            stats["received"] += 1
            try:
                data = json.loads(str(msg))
                user_id = data.get("userId") or data.get("user_id") or "__unknown__"
                turn_id = data.get("turnId") or data.get("turn_id") or ""
                content = data.get("content") or data.get("text") or ""
                role = data.get("role", "user")
                source = data.get("source", "browser-plugin")
                created_at = data.get("createdAt") or data.get("created_at") or time.time()

                # 1. Insert to per-user DB
                thought_text = data.get("thought_text", "")
                is_dev_claude = source == "dev-claude-cli-mac"

                meta_dict = {"source": source, "ingested_via": "servicebus"}
                if is_dev_claude:
                    meta_dict.update({
                        "session_id": data.get("session_id", ""),
                        "model": data.get("model", ""),
                        "cwd": data.get("cwd", ""),
                        "version": data.get("version", ""),
                        "gitBranch": data.get("gitBranch", ""),
                    })

                with get_db_connection(user_id=user_id) as conn:
                    # Insert reasoning row (for dev-claude thinking blocks)
                    if thought_text:
                        conn.execute(
                            "INSERT INTO InteractionLogs "
                            "(user_id, turn_id, role, content, meta, created_at) "
                            "VALUES (?, ?, 'reasoning', ?, ?, ?)",
                            (user_id, turn_id, thought_text,
                             json.dumps(meta_dict, ensure_ascii=False),
                             created_at),
                        )
                    # Insert main content row
                    conn.execute(
                        "INSERT INTO InteractionLogs "
                        "(user_id, turn_id, role, content, thought_text, meta, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (user_id, turn_id, role, content, thought_text or None,
                         json.dumps(meta_dict, ensure_ascii=False),
                         created_at),
                    )

                # 2. Archive raw message to Blob
                await _archive_to_blob(data, user_id)

                # 3. Extract concepts → Concept + ConceptRelation (best-effort)
                if content:
                    _ingest_concepts_to_graph(content, source)

                # 4. Complete
                receiver.complete_message(msg)
                stats["ingested"] += 1

            except Exception as e:
                err_str = str(e)
                if "database is locked" in err_str.lower():
                    # DB contention -> abandon, retry next cron
                    receiver.abandon_message(msg)
                    stats["abandoned"] += 1
                    logger.info(f"[sb_ingest] DB locked, abandoned msg for {user_id}")
                else:
                    logger.error(f"[sb_ingest] error: {e}")
                    receiver.abandon_message(msg)
                    stats["errors"] += 1

        client.close()
    except Exception as e:
        logger.error(f"[sb_ingest] Service Bus client error: {e}")

    if stats["ingested"] > 0:
        logger.info(f"[sb_ingest] {stats}")

    return stats


async def _archive_to_blob(data: dict, user_id: str):
    """Store raw Service Bus message JSON to Blob Storage."""
    if not _BLOB_CONN_STR:
        return

    try:
        from azure.storage.blob import BlobServiceClient

        ts = datetime.now(timezone.utc)
        msg_id = data.get("messageId") or data.get("message_id") or str(time.time())
        blob_name = f"{user_id}/{ts.strftime('%Y-%m-%d')}/{msg_id}.json"

        service = BlobServiceClient.from_connection_string(_BLOB_CONN_STR)
        container = service.get_container_client(_BLOB_CONTAINER)
        try:
            container.create_container()
        except Exception:
            pass  # already exists

        blob = container.get_blob_client(blob_name)
        blob.upload_blob(
            json.dumps(data, ensure_ascii=False, default=str),
            overwrite=True,
        )
    except Exception as e:
        logger.warning(f"[sb_ingest] Blob archive failed: {e}")

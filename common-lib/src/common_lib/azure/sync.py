"""
azure/sync | Azure Table Storage sync: log replication, Service Bus segment triggers, Blob tier policy | needs:azure-data-tables,azure-servicebus,azure-storage-blob | query_logs_from_azure_by_user(),sync_logs_to_table_storage(),sync_segments_to_table_storage(),send_segment_finalize_message(),receive_segment_finalize_messages(),apply_blob_tier_policy(),download_keys_db(),upload_keys_db()
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone

from app.db import get_db_connection, _USER_DB_DIR

# Azure 설정 (환경 변수에서 로드)
AZURE_TABLE_CONNECTION_STRING = os.getenv("AZURE_TABLE_CONNECTION_STRING")
AZURE_SERVICE_BUS_CONNECTION_STRING = os.getenv("SERVICE_BUS_CONNECTION_STRING")
AZURE_BLOB_CONNECTION_STRING = os.getenv("AZURE_BLOB_CONNECTION_STRING")


# --- Table Storage ---

async def query_logs_from_azure_by_user(user_id: str, year_month: str) -> list[dict]:
    """
    Azure Table Storage에서 특정 사용자의 월별 로그 조회 (메인 저장소)
    PartitionKey = {yyyymm}_{shard:02d}_{user_id} → 단 1개 파티션 스캔
    """
    if not AZURE_TABLE_CONNECTION_STRING:
        return []
    from azure.data.tables import TableClient
    from app.id_utils import get_deterministic_shard

    shard = get_deterministic_shard(user_id)
    partition_key = f"{year_month}_{shard:02d}_{user_id}"
    table_client = TableClient.from_connection_string(
        AZURE_TABLE_CONNECTION_STRING, table_name="InteractionLogs"
    )
    entities = table_client.query_entities(f"PartitionKey eq '{partition_key}'")
    return [dict(e) for e in entities]


async def sync_logs_to_table_storage(days_back: int = 1) -> int:
    """
    InteractionLogs를 Azure Table Storage로 이관.
    반환: 이관된 로그 수
    """
    if not AZURE_TABLE_CONNECTION_STRING:
        print("[Azure Table] 연결 문자열 미설정")
        return 0

    from azure.data.tables import TableClient, TableEntity
    from app.id_utils import get_azure_partition_key, get_azure_row_key

    cutoff = time.time() - days_back * 86400
    all_rows = []

    # Scan common DB
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM InteractionLogs WHERE created_at > ? ORDER BY created_at",
            (cutoff,),
        ).fetchall()
        all_rows.extend(rows)

    # Scan per-user DBs
    if os.path.isdir(_USER_DB_DIR):
        import sqlite3
        for fname in sorted(os.listdir(_USER_DB_DIR)):
            if not fname.endswith(".db"):
                continue
            db_path = os.path.join(_USER_DB_DIR, fname)
            try:
                conn2 = sqlite3.connect(db_path, timeout=5.0)
                conn2.row_factory = sqlite3.Row
                rows2 = conn2.execute(
                    "SELECT * FROM InteractionLogs WHERE created_at > ? ORDER BY created_at",
                    (cutoff,),
                ).fetchall()
                all_rows.extend(rows2)
                conn2.close()
            except Exception:
                pass

    if not all_rows:
        return 0

    table_client = TableClient.from_connection_string(
        AZURE_TABLE_CONNECTION_STRING, table_name="InteractionLogs"
    )

    count = 0
    for row in all_rows:
        log_id = row.get("log_id") or str(row["turn_id"])
        entity = TableEntity({
            "PartitionKey": get_azure_partition_key(row["user_id"] or "__unknown__", log_id),
            "RowKey": get_azure_row_key(log_id, row["role"]),
            "content": (row["content"] or "")[:30000],
            "role": row["role"] or "",
            "feedback_score": row["feedback_score"] or 0.0,
            "error_type": row["error_type"] or "",
            "turn_interval_sec": row["turn_interval_sec"] or 0.0,
            "reference_blob_path": row["reference_blob_path"] or "",
            "created_at": datetime.fromtimestamp(row["created_at"], tz=timezone.utc).isoformat(),
        })
        try:
            table_client.upsert_entity(entity)
            count += 1
        except Exception as e:
            print(f"[Azure Table] upsert 실패: {e}")

    print(f"[Azure Table] {count}개 로그 이관 완료")
    return count


async def sync_segments_to_table_storage() -> int:
    """
    ConversationSegments를 Azure Table Storage로 이관.
    """
    if not AZURE_TABLE_CONNECTION_STRING:
        return 0

    from azure.data.tables import TableClient, TableEntity

    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM ConversationSegments ORDER BY created_at"
        ).fetchall()

    if not rows:
        return 0

    table_client = TableClient.from_connection_string(
        AZURE_TABLE_CONNECTION_STRING, table_name="ConversationSegments"
    )

    count = 0
    for row in rows:
        entity = TableEntity({
            "PartitionKey": row["user_id"] or "__unknown__",
            "RowKey": row["segment_id"],
            "summary": (row["summary"] or "")[:30000],
            "summary_status": row["summary_status"] or "",
            "created_at": datetime.fromtimestamp(row["created_at"], tz=timezone.utc).isoformat(),
        })
        try:
            table_client.upsert_entity(entity)
            count += 1
        except Exception as e:
            print(f"[Azure Table Segment] upsert 실패: {e}")

    print(f"[Azure Table] {count}개 세그먼트 이관 완료")
    return count


# --- Service Bus ---

async def send_segment_finalize_message(segment_id: str, user_id: str) -> bool:
    """
    Service Bus 큐로 세그먼트 종료 메시지 전송.
    """
    if not AZURE_SERVICE_BUS_CONNECTION_STRING:
        print("[Service Bus] 연결 문자열 미설정")
        return False

    from azure.servicebus import ServiceBusClient, ServiceBusMessage

    try:
        servicebus_client = ServiceBusClient.from_connection_string(
            AZURE_SERVICE_BUS_CONNECTION_STRING
        )
        sender = servicebus_client.get_queue_sender(queue_name="segment-finalize")
        message = ServiceBusMessage(
            json.dumps({"segment_id": segment_id, "user_id": user_id})
        )
        sender.send_messages(message)
        print(f"[Service Bus] segment_finalize 메시지 전송: {segment_id}")
        return True
    except Exception as e:
        print(f"[Service Bus] 전송 실패: {e}")
        return False


async def receive_segment_finalize_messages(max_messages: int = 10) -> list[dict]:
    """
    Service Bus 큐에서 세그먼트 종료 메시지 수신.
    """
    if not AZURE_SERVICE_BUS_CONNECTION_STRING:
        return []

    from azure.servicebus import ServiceBusClient

    messages = []
    try:
        servicebus_client = ServiceBusClient.from_connection_string(
            AZURE_SERVICE_BUS_CONNECTION_STRING
        )
        receiver = servicebus_client.get_queue_receiver(queue_name="segment-finalize")
        received = receiver.receive_messages(max_message_count=max_messages, max_wait_time=5)
        for msg in received:
            try:
                data = json.loads(str(msg))
                messages.append(data)
            except json.JSONDecodeError:
                pass
            receiver.complete_message(msg)
        print(f"[Service Bus] {len(messages)}개 메시지 수신")
    except Exception as e:
        print(f"[Service Bus] 수신 실패: {e}")

    return messages


# --- Blob Storage 계층 정책 ---

async def apply_blob_tier_policy(days_threshold: int = 2190) -> int:
    """
    Blob Storage 계층 정책: 6년(2190일) 이상 데이터 Cold 이동.
    반환: 이동된 Blob 수
    """
    if not AZURE_BLOB_CONNECTION_STRING:
        print("[Blob Tier] 연결 문자열 미설정")
        return 0

    from azure.storage.blob import BlobServiceClient, BlobTier

    try:
        blob_service_client = BlobServiceClient.from_connection_string(
            AZURE_BLOB_CONNECTION_STRING
        )
    except Exception as e:
        print(f"[Blob Tier] 클라이언트 생성 실패: {e}")
        return 0

    count = 0
    threshold_date = datetime.now(timezone.utc) - timedelta(days=days_threshold)

    try:
        containers = blob_service_client.list_containers()
        for container in containers:
            container_client = blob_service_client.get_container_client(container.name)
            blobs = container_client.list_blobs()
            for blob in blobs:
                if blob.last_modified and blob.last_modified < threshold_date:
                    if blob.blob_tier != BlobTier.COOL:
                        blob_client = container_client.get_blob_client(blob.name)
                        blob_client.set_standard_blob_tier(BlobTier.COOL)
                        count += 1
    except Exception as e:
        print(f"[Blob Tier] 정책 적용 실패: {e}")

    print(f"[Blob Tier] {count}개 Blob Cold 이동 완료")
    return count


# --- KeyStore DB 동기화 (Blob Storage) ---

_KEY_DB_BLOB_CONTAINER = "seedling-config"
_KEY_DB_BLOB_NAME = "gemini_keys.db"


def _get_blob_client():
    """Blob 클라이언트 생성 (연결 문자열 없으면 None)"""
    conn = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    if not conn:
        return None
    from azure.storage.blob import BlobServiceClient
    from azure.core.pipeline.policies import RetryPolicy
    # 3초 타임아웃 정책
    retry_policy = RetryPolicy(timeout=3)
    service = BlobServiceClient.from_connection_string(
        conn,
        retry_policy=retry_policy,
        connection_timeout=3,
        read_timeout=3,
    )
    container = service.get_container_client(_KEY_DB_BLOB_CONTAINER)
    try:
        container.create_container()
    except Exception:
        pass  # 이미 존재
    return container.get_blob_client(_KEY_DB_BLOB_NAME)


def download_keys_db(local_path: str = "/data/keys/gemini.db") -> bool:
    """Azure Blob → 로컬 DB 파일 다운로드 (시작 시)

    Returns:
        True (다운로드 성공 또는 Blob 미존재) / False (오류)
    """
    blob = _get_blob_client()
    if not blob:
        return True  # Azure 미설정 시 무시

    try:
        if not blob.exists():
            return True  # 최초 실행: Blob 없으면 로컬에서 시작

        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(blob.download_blob().readall())
        print(f"[Azure Blob Sync] DB 다운로드 완료: {local_path}")
        return True
    except Exception as e:
        print(f"[Azure Blob Sync] DB 다운로드 실패: {e}")
        return False


def upload_keys_db(local_path: str = "/data/keys/gemini.db") -> bool:
    """로컬 DB 파일 → Azure Blob 업로드 (키 변경 시)

    Returns:
        True (업로드 성공 또는 Azure 미설정) / False (오류)
    """
    blob = _get_blob_client()
    if not blob:
        return True  # Azure 미설정 시 무시

    try:
        if not os.path.exists(local_path):
            return True  # DB 파일 없으면 무시

        with open(local_path, "rb") as f:
            blob.upload_blob(f, overwrite=True)
        print(f"[Azure Blob Sync] DB 업로드 완료: {local_path}")
        return True
    except Exception as e:
        print(f"[Azure Blob Sync] DB 업로드 실패: {e}")
        return False


from app.azure_cosmos import (
    sync_daily_metrics_to_cosmos,
    sync_strategic_decisions_to_cosmos,
    sync_user_profile_to_cosmos,
)

# --- 주기별 실행 ---

async def run_azure_sync():
    """매일 KST 05:00 실행"""
    log_count = await sync_logs_to_table_storage()
    seg_count = await sync_segments_to_table_storage()
    blob_count = await apply_blob_tier_policy()
    # Cosmos DB 싱크
    cosmos_metrics = await sync_daily_metrics_to_cosmos()
    cosmos_decisions = await sync_strategic_decisions_to_cosmos()
    cosmos_profiles = await sync_user_profile_to_cosmos()
    print(f"[Azure Sync] 로그 {log_count}개, 세그먼트 {seg_count}개, Blob {blob_count}개, "
          f"Cosmos: 메트릭 {cosmos_metrics}개, 결정 {cosmos_decisions}개, 프로필 {cosmos_profiles}개")

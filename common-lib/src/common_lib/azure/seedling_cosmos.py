"""
azure/seedling_cosmos | Cosmos DB free-tier sync: SQLite metadata migration to Cosmos DB | needs:azure-cosmos | sync_daily_metrics_to_cosmos(),sync_strategic_decisions_to_cosmos(),sync_user_profile_to_cosmos(),read_daily_metrics_from_cosmos()
"""
import os
from datetime import datetime, timezone

from app.db import get_db_connection

_COSMOS_URL = os.getenv("COSMOS_DB_URL")
_COSMOS_KEY = os.getenv("COSMOS_DB_KEY")
_COSMOS_DATABASE = os.getenv("COSMOS_DB_DATABASE", "seedling-meta")


def _get_cosmos_container(container_name: str):
    """Cosmos DB 컨테이너 클라이언트 반환"""
    if not _COSMOS_URL or not _COSMOS_KEY:
        return None
    from azure.cosmos import CosmosClient
    client = CosmosClient(_COSMOS_URL, credential=_COSMOS_KEY)
    database = client.get_database_client(_COSMOS_DATABASE)
    return database.get_container_client(container_name)


async def sync_daily_metrics_to_cosmos(date: str | None = None) -> int:
    """
    DailyMetrics를 Cosmos DB로 싱크 (SQLite → Cosmos DB).
    반환: 싱크된 레코드 수
    """
    container = _get_cosmos_container("DailyMetrics")
    if not container:
        print("[Cosmos DB] 연결 문자열 미설정")
        return 0

    with get_db_connection() as conn:
        if date:
            rows = conn.execute(
                "SELECT * FROM DailyMetrics WHERE date = ?", (date,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM DailyMetrics ORDER BY date DESC LIMIT 30"
            ).fetchall()

    if not rows:
        return 0

    count = 0
    for row in rows:
        item = {
            "id": row["date"],
            "date": row["date"],
            "ece": row["ece"],
            "overconfidence_rate": row["overconfidence_rate"],
            "underconfidence_rate": row["underconfidence_rate"],
            "flash_delegation_rate": row["flash_delegation_rate"],
            "pro_promotion_rate": row["pro_promotion_rate"],
            "complexity_accuracy": row["complexity_accuracy"],
            "cache_hit_rate": row["cache_hit_rate"],
            "sentiment_best_pct": row["sentiment_best_pct"],
            "sentiment_good_pct": row["sentiment_good_pct"],
            "sentiment_bad_pct": row["sentiment_bad_pct"],
            "sentiment_worst_pct": row["sentiment_worst_pct"],
            "session_continuation_rate": row["session_continuation_rate"],
            "avg_latency_ms": row["avg_latency_ms"],
            "tir_avg_ms": row["tir_avg_ms"],
        }
        try:
            container.upsert_item(item)
            count += 1
        except Exception as e:
            print(f"[Cosmos DB DailyMetrics] upsert 실패: {e}")

    print(f"[Cosmos DB] DailyMetrics {count}개 싱크 완료")
    return count


async def sync_strategic_decisions_to_cosmos(limit: int = 50) -> int:
    """
    StrategicDecisions를 Cosmos DB로 싱크.
    """
    container = _get_cosmos_container("StrategicDecisions")
    if not container:
        return 0

    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM StrategicDecisions ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    if not rows:
        return 0

    count = 0
    for row in rows:
        item = {
            "id": row["decision_id"],
            "decision_id": row["decision_id"],
            "report_period": row["report_period"],
            "category": row["category"],
            "recommendation_type": row["recommendation_type"],
            "current_value": row["current_value"],
            "recommended_value": row["recommended_value"],
            "operator_decision": row["operator_decision"],
            "operator_applied_value": row["operator_applied_value"],
            "evidence_logs": row["evidence_logs"],
            "verification_result": row["verification_result"],
            "applied_at": row["applied_at"],
            "created_at": row["created_at"],
        }
        try:
            container.upsert_item(item)
            count += 1
        except Exception as e:
            print(f"[Cosmos DB StrategicDecisions] upsert 실패: {e}")

    print(f"[Cosmos DB] StrategicDecisions {count}개 싱크 완료")
    return count


async def sync_user_profile_to_cosmos(user_id: str | None = None) -> int:
    """
    UserProfile을 Cosmos DB로 싱크.
    """
    container = _get_cosmos_container("UserProfile")
    if not container:
        return 0

    with get_db_connection() as conn:
        if user_id:
            rows = conn.execute(
                "SELECT * FROM UserProfile WHERE user_id = ?", (user_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM UserProfile ORDER BY last_updated DESC LIMIT 100"
            ).fetchall()

    if not rows:
        return 0

    count = 0
    for row in rows:
        item = {
            "id": row["user_id"],
            "user_id": row["user_id"],
            "preferences": row["preferences"],
            "knowledge_level": row["knowledge_level"],
            "interaction_style": row["interaction_style"],
            "key_decisions": row["key_decisions"],
            "recurring_topics": row["recurring_topics"],
            "last_updated": row["last_updated"],
        }
        try:
            container.upsert_item(item)
            count += 1
        except Exception as e:
            print(f"[Cosmos DB UserProfile] upsert 실패: {e}")

    print(f"[Cosmos DB] UserProfile {count}개 싱크 완료")
    return count


async def read_daily_metrics_from_cosmos(date: str) -> dict | None:
    """Cosmos DB에서 DailyMetrics 조회 (읽기 전용)"""
    container = _get_cosmos_container("DailyMetrics")
    if not container:
        return None
    try:
        item = container.read_item(item=date, partition_key=date)
        return dict(item)
    except Exception:
        return None

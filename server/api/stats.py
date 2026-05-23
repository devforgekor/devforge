import logging
from collections import deque
from typing import Any, Dict, List

from .async_pg import get_pool

logger = logging.getLogger(__name__)

_api_latency: deque = deque(maxlen=1000)

RANGE_DAYS = {
    "1d": 1,
    "7d": 7,
    "30d": 30,
    "all": 36500,
}


def record_api_call(ms: float) -> None:
    _api_latency.append(ms)


def get_api_stats() -> Dict[str, Any]:
    if not _api_latency:
        return {"total_requests": 0, "avg_response_ms": 0, "max_response_ms": 0, "p95_response_ms": 0}
    sorted_lat = sorted(_api_latency)
    n = len(sorted_lat)
    return {
        "total_requests": n,
        "avg_response_ms": round(sum(sorted_lat) / n),
        "max_response_ms": round(max(sorted_lat)),
        "p95_response_ms": round(sorted_lat[int(n * 0.95)]),
    }


async def get_stats(range_str: str = "7d") -> Dict[str, Any]:
    days = RANGE_DAYS.get(range_str, 7)
    pool = await get_pool()
    async with pool.acquire() as conn:
        overview = await _query_overview(conn)
        period = await _query_period(conn, days)
        ingest = await _query_ingest(conn)
        tools_data = await _query_tools(conn, days)
        daily = await _query_daily(conn, days)
        system = await _query_system(conn, period)

    return {
        "overview": overview,
        "period": period,
        "ingest": ingest,
        "tools": tools_data,
        "api": get_api_stats(),
        "daily": daily,
        "system": system,
    }


async def _query_overview(conn) -> Dict[str, Any]:
    row = await conn.fetchrow("SELECT COUNT(*) AS c FROM conversations")
    total_conv = row["c"]
    row = await conn.fetchrow("SELECT COUNT(*) AS c FROM turns")
    total_turns = row["c"]
    row = await conn.fetchrow("SELECT COUNT(*) AS c FROM obs_dec")
    total_dec = row["c"]

    sources = await conn.fetch(
        "SELECT source, COUNT(*) AS cnt FROM conversations GROUP BY source ORDER BY cnt DESC"
    )
    models = await conn.fetch(
        "SELECT model, COUNT(*) AS cnt FROM conversations WHERE model IS NOT NULL GROUP BY model ORDER BY cnt DESC"
    )
    return {
        "total_conversations": total_conv,
        "total_turns": total_turns,
        "total_decisions": total_dec,
        "sources": {r["source"]: r["cnt"] for r in sources},
        "models": {r["model"]: r["cnt"] for r in models},
    }


async def _query_period(conn, days: int) -> Dict[str, Any]:
    row = await conn.fetchrow(
        """
        SELECT COUNT(*) AS turns,
               COALESCE(SUM((meta->>'tokens')::int), 0) AS total_tokens,
               COALESCE(AVG((meta->>'latency_ms')::int), 0) AS avg_latency_ms,
               COALESCE(MAX((meta->>'latency_ms')::int), 0) AS max_latency_ms,
               COALESCE(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY (meta->>'latency_ms')::int), 0) AS p95_latency_ms,
               COUNT(*) FILTER (WHERE (meta->>'latency_ms')::int > 2000) AS slow_queries
        FROM turns
        WHERE created_at > now() - make_interval(days => $1::int)
          AND meta ? 'tokens'
          AND meta ? 'latency_ms'
        """,
        days,
    )
    return {
        "range": f"{days}d",
        "turns": row["turns"],
        "total_tokens": row["total_tokens"],
        "avg_latency_ms": row["avg_latency_ms"],
        "max_latency_ms": row["max_latency_ms"],
        "p95_latency_ms": int(row["p95_latency_ms"]),
        "slow_queries": row["slow_queries"],
    }


async def _query_ingest(conn) -> Dict[str, Any]:
    rows = await conn.fetch(
        """
        SELECT c.source,
               COUNT(*) FILTER (WHERE t.created_at > now() - interval '1 day') AS today,
               COUNT(*) FILTER (WHERE t.created_at > now() - interval '7 days') AS this_week
        FROM turns t
        JOIN conversations c ON t.conversation_id = c.id
        WHERE t.created_at > now() - interval '7 days'
        GROUP BY c.source
        ORDER BY this_week DESC
        """
    )
    by_source = {}
    today_total = 0
    week_total = 0
    for r in rows:
        by_source[r["source"]] = {"today": r["today"], "this_week": r["this_week"]}
        today_total += r["today"]
        week_total += r["this_week"]
    return {"today": today_total, "this_week": week_total, "by_source": by_source}


async def _query_tools(conn, days: int) -> Dict[str, Any]:
    rows = await conn.fetch(
        """
        SELECT tool, COUNT(*) AS cnt
        FROM turns, jsonb_array_elements_text(meta->'tools_used') AS tool
        WHERE created_at > now() - make_interval(days => $1::int)
        GROUP BY tool ORDER BY cnt DESC LIMIT 10
        """,
        days,
    )
    type_rows = await conn.fetch(
        """
        SELECT meta->>'type' AS type, COUNT(*) AS cnt
        FROM turns
        WHERE created_at > now() - make_interval(days => $1::int)
          AND meta->>'type' IS NOT NULL
        GROUP BY type ORDER BY cnt DESC
        """,
        days,
    )
    return {
        "top_used": [[r["tool"], r["cnt"]] for r in rows],
        "type_breakdown": {r["type"]: r["cnt"] for r in type_rows},
    }


async def _query_daily(conn, days: int) -> List[Dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT created_at::date AS date, COUNT(*) AS turns,
               COALESCE(SUM((meta->>'tokens')::int), 0) AS tokens
        FROM turns
        WHERE created_at > now() - make_interval(days => $1::int)
        GROUP BY date ORDER BY date
        """,
        days,
    )
    return [{"date": str(r["date"]), "turns": r["turns"], "tokens": r["tokens"]} for r in rows]


async def _query_system(conn, period: Dict) -> Dict[str, Any]:
    row = await conn.fetchrow(
        "SELECT pg_size_pretty(pg_database_size('devforge_app')) AS db_size,"
        "       pg_database_size('devforge_app') AS db_size_bytes"
    )
    db_size = row["db_size"]
    db_size_bytes = row["db_size_bytes"]
    row = await conn.fetchrow(
        "SELECT COUNT(*) AS cnt FROM pg_stat_activity WHERE datname = 'devforge_app'"
    )
    active_conns = row["cnt"]
    db_size_pct = round(db_size_bytes / (30 * 1024 * 1024 * 1024) * 100, 2)

    pool = await get_pool()
    pool_size = pool._maxsize

    warnings = _build_warnings(db_size_pct, period, active_conns, pool_size)

    return {
        "db_size": db_size,
        "db_size_pct": db_size_pct,
        "pool_size": pool_size,
        "active_connections": active_conns,
        "health": "degraded" if warnings else "ok",
        "warnings": warnings,
    }


def _build_warnings(
    db_pct: float,
    period: Dict,
    conns: int,
    pool_size: int,
) -> List[str]:
    warnings: List[str] = []
    if db_pct > 80:
        warnings.append(f"DB size {db_pct:.1f}% of 30GB LVM")
    if period["p95_latency_ms"] and period["p95_latency_ms"] > 2000:
        warnings.append(f"P95 LLM latency {period['p95_latency_ms']}ms exceeds 2000ms")
    if period["turns"] > 0 and period["slow_queries"] > period["turns"] * 0.1:
        warnings.append(
            f"Slow queries {period['slow_queries']}/{period['turns']} exceed 10%"
        )
    if conns >= pool_size * 0.9:
        warnings.append(f"Connection pool near limit: {conns}/{pool_size}")
    return warnings

"""queue_writer — enqueue items to activity_log for downstream review pipeline.

Shared by review_worker.py (fact extraction) and local_debate.py (debate results).
Night batch consumer reads activity_log WHERE queue_status='unprocessed'.
"""

import json
from typing import Any, Dict, List, Optional

from lib.db import psql_ok, esc_sql


def enqueue_review(
    *,
    entry_type: str,
    source: str,
    title: str,
    summary: str,
    body: Dict[str, Any],
    model: str = "",
    turn_ids: Optional[List[str]] = None,
    tags: Optional[List[str]] = None,
) -> bool:
    """Insert a review item into activity_log queue.

    queue_status='unprocessed' — consumed by night batch (14B→32B).
    exec_status='DONE' — source pipeline already completed its work.
    summary_status='raw' — not yet summarized (night batch handles this).
    """
    body_json = json.dumps(body, ensure_ascii=False)
    body_esc = body_json.replace("'", "''")

    tags_sql = "'{}'"
    if tags:
        tags_sql = "ARRAY[" + ",".join(f"'{esc_sql(t)}'" for t in tags) + "]"

    turn_sql = "'{}'"
    if turn_ids:
        turn_sql = "ARRAY[" + ",".join(f"'{tid}'" for tid in turn_ids) + "]::UUID[]"

    model_esc = esc_sql(model) if model else ""

    sql = (
        f"INSERT INTO activity_log "
        f"(type, source, title, summary, body, model, turn_ids, tags, "
        f" summary_status, queue_status, exec_status) "
        f"VALUES ("
        f"'{esc_sql(entry_type)}', "
        f"'{esc_sql(source)}', "
        f"'{esc_sql(title)}', "
        f"'{esc_sql(summary)}', "
        f"'{body_esc}', "
        f"'{model_esc}', "
        f"{turn_sql}, "
        f"{tags_sql}, "
        f"'raw', 'unprocessed', 'DONE'"
        f")"
    )
    return psql_ok(sql)

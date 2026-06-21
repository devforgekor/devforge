#!/bin/bash
# DevForge Embed Runner — continuous embed_batch.py loop
# Progress: catchdog_events DB 5min, Slack 30min
set -e
EMBED_PY="/opt/projects/server/scripts/pipelines/embed_batch.py"
CHECKPOINT="embed_batch"
cd /opt/projects/server

echo "[embed-runner] Start $(date -u +%Y-%m-%dT%H:%M:%SZ)"
cd /opt/projects/server/scripts
python3 -c "from lib.protection import register_protect; register_protect('embed_runner_sh', reason='shell-based embed runner', ports=[8081])" 2>/dev/null || true
cd /opt/projects/server

trap "echo '[embed-runner] Stopped'; cd /opt/projects/server/scripts && python3 -c \"from lib.protection import unregister_protect; unregister_protect('embed_runner_sh')\" 2>/dev/null; cd /opt/projects/server; exit 0" TERM INT

cycle=0
last_event=0
last_slack=0

while true; do
    cycle=$((cycle + 1))

    # Check remaining
    remaining=$(podman exec postgres psql -U devforge -d devforge_app -tAc \
        "SELECT COUNT(*) FROM turns t LEFT JOIN embeddings e ON e.source_type='turn' AND e.source_id=t.id AND e.model_name='qwen3-embedding-8b-v1' WHERE e.id IS NULL" 2>/dev/null || echo "0")
    total_done=$(podman exec postgres psql -U devforge -d devforge_app -tAc \
        "SELECT COUNT(*) FROM turns t JOIN embeddings e ON e.source_type='turn' AND e.source_id=t.id AND e.model_name='qwen3-embedding-8b-v1'" 2>/dev/null || echo "0")
    total=$(podman exec postgres psql -U devforge -d devforge_app -tAc \
        "SELECT COUNT(*) FROM turns" 2>/dev/null || echo "0")

    if [ "$remaining" = "0" ] || [ -z "$remaining" ]; then
        echo "[embed-runner] All done! $total/$total embedded"
        podman exec postgres psql -U devforge -d devforge_app -c \
            "INSERT INTO catchdog_events (component, event_type, detail) VALUES ('embed_batch', 'complete', 'All $total turns embedded')" 2>/dev/null || true
        cd /opt/projects/server/scripts && python3 -c "from lib.protection import unregister_protect; unregister_protect('embed_runner_sh')" 2>/dev/null; cd /opt/projects/server
        exit 0
    fi

    pct=$(python3 -c "print(f'{100*$total_done/$total:.1f}')" 2>/dev/null || echo "?")
    echo "[embed-runner] Cycle $cycle: $remaining remain, $total_done/$total ($pct%)"

    # Run batch
    python3 -u "$EMBED_PY" --limit 200 2>&1 | while IFS= read -r line; do
        echo "  $line"
    done
    rc=${PIPESTATUS[0]}

    if [ "$rc" -ne 0 ]; then
        echo "[embed-runner] embed_batch exit=$rc, continuing"
    fi

    # Catchdog event every 300s
    now=$(date +%s)
    if [ $((now - last_event)) -ge 300 ] || [ "$cycle" -eq 1 ]; then
        podman exec postgres psql -U devforge -d devforge_app -c \
            "INSERT INTO catchdog_events (component, event_type, detail) VALUES ('embed_batch', 'progress', 'Cycle $cycle: $total_done/$total ($pct%%)')" 2>/dev/null || true
        last_event=$now
    fi

    # Slack every 1800s
    if [ $((now - last_slack)) -ge 1800 ] || [ "$cycle" -eq 1 ]; then
        bar_len=16
        filled=$((bar_len * total_done / total))
        bar=$(python3 -c "print('█'*$filled + '░'*($bar_len-$filled))")
        podman exec postgres psql -U devforge -d devforge_app -c \
            "SELECT 'x'" 2>/dev/null > /dev/null
        cd /opt/projects/server/scripts && python3 -c "
from lib.pipeline_common import slack_send
slack_send('*Embed Progress* — Qwen3-Embedding-8B\n${bar} ${pct}%  (${total_done}/${total} 완료, ${remaining} 남음)')
" || echo "[embed-runner] Slack send failed"; cd /opt/projects/server
        last_slack=$now
    fi

    # Brief pause to prevent tight loop on rapid failure
    sleep 2
done

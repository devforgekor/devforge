#!/usr/bin/env python3
# Status: production
# Path: systemd:baseline-daily.timer
"""W1 Baseline Daily Runner — D2-D7."""
import json, os, subprocess, datetime

OUTDIR = "/opt/projects/server/docs/ops/baseline"
DB = 'podman exec -i postgres psql -U postgres -d devforge_app -t -A'

def run(sql):
    r = subprocess.run(f'{DB} -c "{sql}"', shell=True, capture_output=True, text=True)
    return r.stdout.strip()

def run_rows(sql):
    r = subprocess.run(f'{DB} -t -A -c "{sql}"', shell=True, capture_output=True, text=True)
    if not r.stdout.strip():
        return {}
    result = {}
    for line in r.stdout.strip().splitlines():
        parts = line.split("|")
        if len(parts) == 2:
            result[parts[0]] = parts[1]
    return result

def collect():
    today = datetime.date.today().isoformat()
    out = {"date": today, "mode": "daily"}

    # 1. turns rate
    out["turns_24h"] = run("SELECT count(*) FROM turns WHERE created_at >= NOW() - INTERVAL '24 hours'")
    out["turns_total"] = run("SELECT count(*) FROM turns")
    out["turns_source"] = run_rows("SELECT source, count(*) FROM turns GROUP BY source")
    out["turns_age_days"] = run("SELECT ROUND(EXTRACT(EPOCH FROM (NOW() - MIN(created_at)))/86400, 1) FROM turns")

    # 2. observations (hook proxy)
    out["observations_24h"] = run("SELECT count(*) FROM observations WHERE created_at >= NOW() - INTERVAL '24 hours'")
    out["observations_7d"] = run("SELECT count(*) FROM observations WHERE created_at >= NOW() - INTERVAL '7 days'")

    # 3. pipeline state
    out["pipeline_state"] = run_rows("SELECT pipeline_state, count(*) FROM turns GROUP BY pipeline_state ORDER BY pipeline_state")

    # 4. service status
    services = ["devforge-turn-watcher", "devforge-day-cycle", "devforge-worker", "devforge-fastapi", "devforge-mcp", "devforge-watchdog"]
    svc_status = {}
    for svc in services:
        r = subprocess.run(["systemctl", "--user", "is-active", f"{svc}.timer"], capture_output=True, text=True)
        svc_status[f"{svc}.timer"] = r.stdout.strip()
        r2 = subprocess.run(["systemctl", "--user", "is-active", f"{svc}.service"], capture_output=True, text=True)
        svc_status[f"{svc}.service"] = r2.stdout.strip()
    out["services"] = svc_status

    # 5. netdata
    try:
        r = subprocess.run(["systemctl", "is-active", "netdata"], capture_output=True, text=True)
        out["netdata"] = r.stdout.strip()
    except Exception:
        out["netdata"] = "unreachable"

    # 6. hook overhead
    r = subprocess.run(["python3", "/opt/projects/server/scripts/hooks/measure-hook-overhead.py"],
                       capture_output=True, text=True, cwd="/opt/projects/server")
    if r.returncode == 0:
        try:
            out["hook_overhead"] = json.loads(r.stdout)
        except Exception:
            out["hook_overhead"] = {"raw": r.stdout[:200]}

    # 7. MCP server health
    try:
        r = subprocess.run(["curl", "-s", "--max-time", "5", "http://127.0.0.1:8000/health"],
                           capture_output=True, text=True, timeout=10)
        out["mcp_health"] = r.stdout.strip() if r.returncode == 0 else "unreachable"
    except Exception:
        out["mcp_health"] = "unreachable"

    return out

if __name__ == "__main__":
    data = collect()
    print(json.dumps(data, indent=2))
    os.makedirs(OUTDIR, exist_ok=True)
    outpath = os.path.join(OUTDIR, f"{data['date']}.json")
    with open(outpath, "w") as f:
        json.dump(data, f, indent=2)
    print(f"\nSaved: {outpath}")

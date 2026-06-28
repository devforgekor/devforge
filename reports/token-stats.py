#!/usr/bin/env python3
"""Token savings report — query observations table for all token usage metrics."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from lib.db import psql_json

_TRUNC_STATS_FILE = os.environ.get(
    "MCP_TRUNC_STATS_FILE", "/opt/projects/server/data/mcp-trunc-stats.jsonl"
)


def q(sql):
    return psql_json(sql) or []


def get_trunc_stats():
    """Read mcp-trunc-proxy stats from JSONL file."""
    stats = []
    try:
        with open(_TRUNC_STATS_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    if "original" in d:
                        stats.append(d)
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return stats


print("=" * 70)
print("TOKEN SAVINGS REPORT")
print("=" * 70)

# 1. Proxy (DeepSeek) usage
proxy = q("""
    SELECT
        COUNT(*) as samples,
        COALESCE(SUM((context->>'input_tokens')::int), 0) as total_input,
        COALESCE(SUM((context->>'output_tokens')::int), 0) as total_output,
        COALESCE(ROUND(AVG((context->>'hit_rate')::numeric), 1), 0) as avg_hit_rate,
        COALESCE(SUM((context->>'cache_read')::int), 0) as total_cache_read,
        COALESCE(SUM((context->>'cache_miss')::int), 0) as total_cache_miss
    FROM observations
    WHERE source = 'proxy:anthropic' AND category = 'usage'
""")
if proxy:
    p = proxy[0]
    print(f"\n📡 DeepSeek Proxy Usage  (samples={p['samples']})")
    print(f"  Input tokens:  {p['total_input']:>10,}")
    print(f"  Output tokens: {p['total_output']:>10,}")
    print(f"  Cache reads:   {p['total_cache_read']:>10,}")
    print(f"  Cache misses:  {p['total_cache_miss']:>10,}")
    print(f"  Cache hit rate: {p['avg_hit_rate']}%")
    input_cost = p["total_input"] * 0.14 / 1_000_000
    output_cost = p["total_output"] * 0.42 / 1_000_000
    cache_savings = p["total_cache_read"] * 0.14 / 1_000_000 * 0.5  # cache is ~50% cheaper
    print(f"  Est. cost:     ${input_cost + output_cost:.5f}")
    print(f"  Est. cache savings: ${cache_savings:.5f}")

# 2. Extract LLM usage
extract = q("""
    SELECT
        COUNT(*) as samples,
        COALESCE(SUM((context->>'prompt_tokens')::int), 0) as total_prompt,
        COALESCE(SUM((context->>'completion_tokens')::int), 0) as total_completion,
        COALESCE(ROUND(AVG((context->>'max_tokens')::int)), 0) as avg_max_tokens,
        COALESCE(ROUND(AVG((context->>'elapsed_ms')::int)), 0) as avg_elapsed_ms,
        (SELECT COUNT(*) FROM observations
         WHERE source = 'pipeline:extract_llm' AND category = 'usage'
           AND context->>'section' = 'user') AS user_count,
        (SELECT COUNT(*) FROM observations
         WHERE source = 'pipeline:extract_llm' AND category = 'usage'
           AND context->>'section' = 'thinking') AS thinking_count,
        (SELECT COUNT(*) FROM observations
         WHERE source = 'pipeline:extract_llm' AND category = 'usage'
           AND context->>'section' = 'text') AS text_count
    FROM observations
    WHERE source = 'pipeline:extract_llm' AND category = 'usage'
""")
if extract and extract[0]["samples"] > 0:
    e = extract[0]
    print(f"\n📝 Extract LLM Usage  (samples={e['samples']})")
    print(f"  Prompt tokens:      {e['total_prompt']:>10,}")
    print(f"  Completion tokens:  {e['total_completion']:>10,}")
    print(f"  Avg max_tokens:     {e['avg_max_tokens']}")
    print(f"  Avg elapsed:        {e['avg_elapsed_ms']}ms")
    print(f"  Sections: user={e['user_count']} think={e['thinking_count']} text={e['text_count']}")
else:
    print(
        "\n📝 Extract LLM Usage: (no data — deploy extract_llm changes and wait for next pipeline run)"
    )

# 3. Tool usage stats — estimate MCP truncation impact
tool_sizes = q("""
    SELECT
        tags->>'tool' as tool,
        COUNT(*) as calls,
        COALESCE(ROUND(AVG((context->>'output_size')::int)), 0) as avg_output_bytes
    FROM observations
    WHERE source = 'hook:PostToolUse'
      AND tags ? 'output_size'
      AND created_at > NOW() - INTERVAL '1 day'
    GROUP BY tags->>'tool'
    ORDER BY calls DESC
""")
print("\n🔧 Tool Output Sizes (from hook measurements)")
if tool_sizes:
    for t in tool_sizes:
        print(
            f"  {t['tool']:12s}  {t['calls']:4d} calls  avg {int(t['avg_output_bytes']) / 1024:6.1f}KB"
        )
else:
    print("  (no data — hook output tracking pending)")

# 4. MCP truncation stats (from patched mcp-trunc-proxy journalctl)
trunc = get_trunc_stats()
print("\n🔧 MCP Truncation Stats (from patched mcp-trunc-proxy)")
if trunc:
    total_original = sum(t["original"] for t in trunc)
    by_tool = {}
    for t in trunc:
        by_tool.setdefault(t["tool"], []).append(t["original"])
    print(f"  Total truncations: {len(trunc)}")
    print(f"  Total offloaded bytes: {total_original:,} ({total_original / 1024:.0f}KB)")
    for tool, sizes in sorted(by_tool.items(), key=lambda x: -len(x[1])):
        avg = sum(sizes) / len(sizes)
        print(f"  {tool:15s}  {len(sizes):4d}x  avg {avg:>8.0f}B ({avg / 1024:.1f}KB)")
else:
    print("  (no data — no truncation events in last 24h)")

# 5. Overall token usage today
today_tokens = q("""
    SELECT
        DATE(created_at) as day,
        SUM((context->>'input_tokens')::int) + SUM((context->>'output_tokens')::int) as total_tokens,
        COUNT(*) as api_calls
    FROM observations
    WHERE source = 'proxy:anthropic' AND category = 'usage'
    GROUP BY DATE(created_at)
    ORDER BY day DESC
    LIMIT 7
""")
print("\n📅 Daily Token Usage (from proxy)")
if today_tokens:
    for d in today_tokens:
        print(f"  {d['day']}: {d['total_tokens']:>10,} tokens  ({d['api_calls']} API calls)")
else:
    print("  (no data yet)")

print("\n" + "=" * 70)
print("token-savior-recall savings not yet measured (no before/after wrapper)")
print("=" * 70)

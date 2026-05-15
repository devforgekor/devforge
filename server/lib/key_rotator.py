"""
Lightweight API Key Rotator — in-memory, no DB dependency.

Usage:
    rotator = KeyRotator([
        ("acct1_key1", "AIza..."),
        ("acct1_key2", "AIza..."),
        ("acct2_key1", "AIza..."),
    ])
    idx, name, key = rotator.pick()
    # ... use key ...
    rotator.success(idx)
    # or on 429:
    rotator.rate_limited(idx, retry_seconds=45)
"""

import time
import random
from datetime import datetime, timezone, timedelta
from typing import Optional


DAILY_QUOTA_THRESHOLD = 300  # seconds — >= 5min = daily quota exhaustion


class KeyRotator:
    """Round-robin key rotation converging to even distribution."""

    def __init__(self, keys: list[tuple[str, str]]):
        """
        keys: [(display_name, api_key), ...]
        """
        self.keys = keys
        self.n = len(keys)

        # in-memory state (no DB)
        self._index = 0
        self._calls: dict[int, int] = {}       # total calls per key
        self._fails: dict[int, int] = {}       # total failures per key
        self._last_used: dict[int, float] = {}  # last use timestamp
        self._backoff_until: dict[int, float] = {}  # backoff expiry

    def pick(self) -> Optional[tuple[int, str, str]]:
        """
        Return (index, name, key) of the next available key.

        Selection priority:
        1. Skip keys in backoff
        2. Among available: fewest fails, then fewest calls, then oldest last_used
        → converges to balanced distribution over time.
        """
        now = time.time()

        # Collect available keys
        available = []
        for i in range(self.n):
            if self._backoff_until.get(i, 0) <= now:
                available.append((
                    self._fails.get(i, 0),
                    self._calls.get(i, 0),
                    self._last_used.get(i, 0),
                    i,
                ))

        if not available:
            return None  # all keys in backoff

        # Sort: fewest fails → fewest calls → oldest last_used
        available.sort()
        idx = available[0][3]

        self._last_used[idx] = now
        return idx, self.keys[idx][0], self.keys[idx][1]

    def wait_seconds(self) -> float:
        """Return seconds until the next key becomes available, or 0."""
        now = time.time()
        waits = [t - now for t in self._backoff_until.values() if t > now]
        return max(waits) if waits else 0.0

    def success(self, idx: int):
        """Record successful call — reset failure counters."""
        self._calls[idx] = self._calls.get(idx, 0) + 1
        self._fails[idx] = 0
        self._backoff_until.pop(idx, None)

    def rate_limited(self, idx: int, retry_seconds: int):
        """
        Record 429 ResourceExhausted.
        - retry_seconds >= DAILY_QUOTA_THRESHOLD → backoff until next day 17:00 KST
        - retry_seconds < DAILY_QUOTA_THRESHOLD → jittered backoff
        """
        self._fails[idx] = self._fails.get(idx, 0) + 1

        if retry_seconds >= DAILY_QUOTA_THRESHOLD:
            # Daily quota exhausted — lock until tomorrow 17:00 KST
            kst = timezone(timedelta(hours=9))
            now_kst = datetime.now(kst)
            today_5pm = now_kst.replace(hour=17, minute=0, second=0, microsecond=0)
            if now_kst >= today_5pm:
                today_5pm += timedelta(days=1)
            self._backoff_until[idx] = today_5pm.timestamp()
        else:
            # Rate-limited — jittered backoff
            jitter = retry_seconds * random.uniform(-0.2, 0.2)
            delay = max(1, retry_seconds + jitter)
            self._backoff_until[idx] = time.time() + delay

    def stats(self) -> dict:
        """Return current rotation stats for monitoring."""
        now = time.time()
        key_stats = []
        for i, (name, _) in enumerate(self.keys):
            backoff_remaining = max(0.0, self._backoff_until.get(i, 0) - now)
            key_stats.append({
                "index": i,
                "name": name,
                "calls": self._calls.get(i, 0),
                "fails": self._fails.get(i, 0),
                "last_used": self._last_used.get(i, 0),
                "in_backoff": backoff_remaining > 0,
                "backoff_remaining": round(backoff_remaining, 1),
            })

        total_calls = sum(ks["calls"] for ks in key_stats)
        avg_calls = total_calls / max(self.n, 1)
        active = [ks for ks in key_stats if not ks["in_backoff"]]

        return {
            "total_keys": self.n,
            "available_keys": len(active),
            "total_calls": total_calls,
            "avg_calls_per_key": round(avg_calls, 1),
            "keys": key_stats,
        }

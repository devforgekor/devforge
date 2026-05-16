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

    def __init__(self, keys: list[tuple[str, str]], state_file: str = ""):
        """
        keys: [(display_name, api_key), ...]
        state_file: optional path to JSON state file for persistence across runs.
        """
        self.keys = keys
        self.n = len(keys)
        self._state_file = state_file

        # in-memory state (no DB)
        self._index = 0
        self._calls: dict[int, int] = {}       # total calls per key
        self._fails: dict[int, int] = {}       # total failures per key
        self._last_used: dict[int, float] = {}  # last use timestamp
        self._backoff_until: dict[int, float] = {}  # backoff expiry

        if state_file:
            self._load_state()

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

    def _load_state(self):
        """Restore rotation state from disk (JSON)."""
        import json
        from pathlib import Path

        path = Path(self._state_file).expanduser()
        if not path.exists():
            return
        try:
            state = json.loads(path.read_text())
            for i_str, v in state.get("_calls", {}).items():
                self._calls[int(i_str)] = v
            for i_str, v in state.get("_fails", {}).items():
                self._fails[int(i_str)] = v
            for i_str, v in state.get("_last_used", {}).items():
                self._last_used[int(i_str)] = v
            for i_str, v in state.get("_backoff_until", {}).items():
                self._backoff_until[int(i_str)] = v
        except Exception:
            pass  # corrupt state → start fresh

    def _save_state(self):
        """Persist rotation state to disk (atomic write)."""
        import json
        import tempfile
        from pathlib import Path

        path = Path(self._state_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)

        state = {
            "_calls": {str(k): v for k, v in self._calls.items()},
            "_fails": {str(k): v for k, v in self._fails.items()},
            "_last_used": {str(k): v for k, v in self._last_used.items()},
            "_backoff_until": {str(k): v for k, v in self._backoff_until.items()},
        }
        payload = json.dumps(state, indent=2, ensure_ascii=False)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        try:
            with open(fd, "w") as f:
                f.write(payload)
            Path(tmp).rename(path)
        except Exception:
            Path(tmp).unlink(missing_ok=True)

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
        if self._state_file:
            self._save_state()

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

        if self._state_file:
            self._save_state()

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

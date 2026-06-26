#!/usr/bin/env python3.11
# Status: production
"""Key loading / rotation for Gemini and Brave APIs."""

import json, os, random, time

STATE_FILE = os.path.expanduser("~/.cache/devforge/gemini_rotator_state.json")
SECRETS = os.path.expanduser("~/.config/devforge/secrets.env")
_BRAVE_STATE_FILE = os.path.expanduser("~/.cache/devforge/brave_rotator_state.json")


def load_keys():
    keys = []
    if os.path.exists(SECRETS):
        with open(SECRETS) as f:
            for line in f:
                line = line.strip()
                if line.startswith("GEMINI_API_KEYS="):
                    raw = line.split("=", 1)[1].strip().strip("\"'")
                    for part in raw.split(","):
                        if ":" in part:
                            name, key = part.split(":", 1)
                            keys.append((name.strip(), key.strip()))
    return keys


def pick_key(keys):
    state = {"calls": {}, "fails": {}, "backoff_until": {}}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
        except Exception:
            pass
    now = time.time()
    candidates = []
    for i, (name, key) in enumerate(keys):
        bu = state.get("backoff_until", {}).get(str(i), 0)
        if now >= bu:
            candidates.append((i, name, key))
    if not candidates:
        idx = min(range(len(keys)), key=lambda i: state.get("backoff_until", {}).get(str(i), 0))
        candidates = [(idx, keys[idx][0], keys[idx][1])]
    idx = random.choice(range(len(candidates)))
    i, name, key = candidates[idx]
    state["calls"][str(i)] = state.get("calls", {}).get(str(i), 0) + 1
    if state["calls"].get(str(i), 0) >= 50:
        state["backoff_until"][str(i)] = now + 3600
        state["calls"][str(i)] = 0
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE + ".tmp", "w") as f:
        json.dump(state, f)
    os.rename(STATE_FILE + ".tmp", STATE_FILE)
    return name, key


def _brave_keys():
    if not os.path.exists(SECRETS):
        return []
    with open(SECRETS) as f:
        for line in f:
            line = line.strip()
            if line.startswith("BRAVE_API_KEYS="):
                raw = line.split("=", 1)[1].strip().strip("\"'")
                keys = []
                for part in raw.split(","):
                    if ":" in part:
                        keys.append(part.split(":", 1)[1])
                return keys
    return []


def _brave_pick_key(keys):
    state = {}
    if os.path.exists(_BRAVE_STATE_FILE):
        try:
            with open(_BRAVE_STATE_FILE) as f:
                state = json.load(f)
        except Exception:
            pass
    idx = state.get("idx", 0) % len(keys)
    state["idx"] = (idx + 1) % len(keys)
    os.makedirs(os.path.dirname(_BRAVE_STATE_FILE), exist_ok=True)
    with open(_BRAVE_STATE_FILE + ".tmp", "w") as f:
        json.dump(state, f)
    os.rename(_BRAVE_STATE_FILE + ".tmp", _BRAVE_STATE_FILE)
    return keys[idx], idx

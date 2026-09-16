#!/usr/bin/env python3
import os
import sys

GROUP_SECRETS = {
    "BRAVE_API_KEYS": (1, 4, "BRAVE_API_KEYS"),
    "CONTEXT7_API_KEYS": (1, 4, "CONTEXT7_API_KEY"),
    "EXA_API_KEYS": (1, 4, "EXA_API_KEYS"),
    "TRAVILY_API_KEYS": (1, 4, "TRAVILY_API_KEYS"),
    "YOUCOM_API_KEYS": (1, 4, "YOUCOM_API_KEYS"),
}

SINGLE_SECRETS = [
    "DATAIMPULSE_PROXY_LIST",
    "DEEPSEEK_API_KEY",
    "DUCKDNS_ACCOUNT",
    "DUCKDNS_TOKEN",
    "GUDOKPIN_API_KEY",
    "MASKPROXY_API_KEY",
    "MASKPROXY_PROXY_LIST",
    "MINIPARK4U_SMTP_PASSWORD",
    "MY_COPILOT_GITHUB_TOKEN_KEY",
    "MY_GITHUB_TOKEN_KEY",
    "NEIS_API_KEY",
    "NOTION_TOKEN_KEY",
    "OPENROUTER_HYEONMINPARK4U_API_KEY",
    "OPENROUTER_MESIDS_API_KEY",
    "OPENROUTER_MINIPARK4U_API_KEY",
    "SLACK_BOT_TOKEN_KEY",
    "SLACK_SIGNING_SECRET_KEY",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_TEST_TOKEN_KEY",
    "TELEGRAM_TOKEN_KEY",
    "VERCEL_ORG_ID",
    "VERCEL_REVALIDATE_TOKEN_KEY",
    "VERCEL_TOKEN_KEY",
]


def generate():
    lines = []
    for group_name, (start, end, prefix) in GROUP_SECRETS.items():
        parts = []
        for i in range(start, end + 1):
            env_key = f"{prefix}_{i}"
            val = os.environ.get(env_key, "")
            parts.append(val)
        combined = ",".join(parts)
        if any(parts):
            lines.append(f"{group_name}={combined}")
    for key in SINGLE_SECRETS:
        val = os.environ.get(key, "")
        if val:
            lines.append(f"{key}={val}")
    return "\n".join(lines) + "\n" if lines else ""


def merge(existing_path):
    existing = {}
    if os.path.exists(existing_path):
        with open(existing_path) as f:
            for line in f:
                line = line.strip()
                if line and "=" in line:
                    k, v = line.split("=", 1)
                    existing[k] = v
    merged = dict(existing)
    github = {}
    for group_name, (start, end, prefix) in GROUP_SECRETS.items():
        parts = []
        for i in range(start, end + 1):
            env_key = f"{prefix}_{i}"
            val = os.environ.get(env_key, "")
            parts.append(val)
        combined = ",".join(parts)
        if any(parts):
            github[group_name] = combined
    for key in SINGLE_SECRETS:
        val = os.environ.get(key, "")
        if val:
            github[key] = val
    merged.update(github)
    return merged


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "generate"
    if mode == "--merge":
        existing_path = sys.argv[2] if len(sys.argv) > 2 else ""
        merged = merge(existing_path)
        for k, v in merged.items():
            print(f"{k}={v}")
    elif mode == "--list":
        all_keys = list(GROUP_SECRETS.keys()) + SINGLE_SECRETS
        print(",".join(all_keys))
    else:
        print(generate(), end="")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
import os
import sys

# Group secrets: each group combines multiple GitHub secrets into one secrets.env key
# Format: secrets.env_key -> list of GitHub secret names
GROUP_SECRETS = {
    "BRAVE_API_KEYS": [
        "BRAVE_MESIDS_API_KEY",
        "BRAVE_MINIPARK4U_API_KEY",
        "BRAVE_HYEONMINPARK4U_API_KEY",
        "BRAVE_PLAYPARK4U_API_KEY",
    ],
    "CONTEXT7_API_KEYS": [
        "CONTEXT7_MESIDS_API_KEY",
        "CONTEXT7_MINIPARK4U_API_KEY",
        "CONTEXT7_HYEONMINPARK4U_API_KEY",
        "CONTEXT7_PLAYPARK4U_API_KEY",
    ],
    "EXA_API_KEYS": [
        "EXA_MESIDS_API_KEY",
        "EXA_MINIPARK4U_API_KEY",
        "EXA_HYEONMINPARK4U_API_KEY",
        "EXA_PLAYPARK4U_API_KEY",
    ],
    "GEMINI_API_KEYS": [
        "GEMINI_MESIDS_API_KEY",
        "GEMINI_MINIPARK4U_API_KEY",
        "GEMINI_HYEONMINPARK4U_API_KEY",
        "GEMINI_PLAYPARK4U_API_KEY",
    ],
    "TRAVILY_API_KEYS": [
        "TRAVILY_MESIDS_GITHUB_API_KEY",
        "TRAVILY_MINIPARK4U_API_KEY",
        "TRAVILY_HYEONMINPARK4U_API_KEY",
        "TRAVILY_PLAYPARK4U_API_KEY",
    ],
    "YOUCOM_API_KEYS": [
        "YOUCOM_MESIDS_API_KEY",
        "YOUCOM_MINIPARK4U_API_KEY",
        "YOUCOM_HYEONMINPARK4U_API_KEY",
        "YOUCOM_PLAYPARK4U_API_KEY",
    ],
    "AZURE_SP_ACCOUNT1_CLIENT_ID": ["AZURE_SP_20137133_CLIENT_ID"],
    "AZURE_SP_ACCOUNT1_CLIENT_SECRET": ["AZURE_SP_20137133_CLIENT_SECRET"],
    "AZURE_SP_ACCOUNT1_TENANT_ID": ["AZURE_SP_20137133_TENANT_ID"],
    "AZURE_SP_ACCOUNT2_CLIENT_ID": ["AZURE_SP_MINIPARK4U_CLIENT_ID"],
    "AZURE_SP_ACCOUNT2_CLIENT_SECRET": ["AZURE_SP_MINIPARK4U_CLIENT_SECRET"],
    "AZURE_SP_ACCOUNT2_TENANT_ID": ["AZURE_SP_MINIPARK4U_TENANT_ID"],
    "AZURE_SP_ACCOUNT3_CLIENT_ID": ["AZURE_SP_KUHWADOCS_CLIENT_ID"],
    "AZURE_SP_ACCOUNT3_CLIENT_SECRET": ["AZURE_SP_KUHWADOCS_CLIENT_SECRET"],
    "AZURE_SP_ACCOUNT3_TENANT_ID": ["AZURE_SP_KUHWADOCS_TENANT_ID"],
    "AZURE_SP_ACCOUNT4_CLIENT_ID": ["AZURE_SP_MESIDS_CLIENT_ID"],
    "AZURE_SP_ACCOUNT4_CLIENT_SECRET": ["AZURE_SP_MESIDS_CLIENT_SECRET"],
    "AZURE_SP_ACCOUNT4_TENANT_ID": ["AZURE_SP_MESIDS_TENANT_ID"],
    "DATAIMPULSE_PROXY_LIST": ["DATAIMPULSE_PROXY_KEY"],
    "DUCKDNS_TOKEN": ["DUCKDNS_TOKEN_KEY"],
    "DROPLR_EMAIL": ["DROPLR_USER"],
    "DROPLR_PASSWORD": ["DROPLR_PASS"],
    "ENCRYPTION_PASSPHRASE": ["DEVFORGE_ENCRYPTION_PASSPHRASE"],
    "HF_TOKEN": ["HUGGINGFACE_API_KEY"],
    "LITELLM_MASTER_KEY": ["DEVFORGE_LITELLM_MASTER_KEY"],
    "MASKPROXY_PROXY_LIST": ["MASKPROXY_PROXY_KEY"],
    "MINIPARK4U_SMTP_PASSWORD": ["GMAIL_SMTP_MINIPARK4U"],
    "POSTGRES_PASSWORD": ["DEVFORGE_POSTGRES_PASSWORD"],
    "ZIGHT_EMAIL": ["ZIGHT_API_KEY"],
}

# Single secrets: 1:1 mapping (secrets.env key = GitHub secret name)
SINGLE_SECRETS = [
    "AZURE_STORAGE_ACCOUNT_KEY",
    "AZURE_STORAGE_ACCOUNT_NAME",
    "AZURE_STORAGE_CONNECTION_STRING",
    "CAPSOLVER_API_KEY",
    "DEEPSEEK_API_KEY",
    "GUDOKPIN_API_KEY",
    "MASKPROXY_API_KEY",
    "MY_COPILOT_GITHUB_TOKEN_KEY",
    "MY_GITHUB_TOKEN_KEY",
    "NEIS_API_KEY",
    "NOTION_TOKEN_KEY",
    "OCI_MESIDS_API_KEY_FINGERPRINT",
    "OCI_MESIDS_REGION",
    "OCI_MESIDS_TENANCY_OCID",
    "OCI_MESIDS_USER_OCID",
    "OPENROUTER_HYEONMINPARK4U_API_KEY",
    "OPENROUTER_MESIDS_API_KEY",
    "OPENROUTER_MINIPARK4U_API_KEY",
    "SLACK_BOT_TOKEN_KEY",
    "SLACK_CHANNEL",
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
    for env_key, github_names in GROUP_SECRETS.items():
        parts = []
        for gh_name in github_names:
            val = os.environ.get(gh_name, "")
            parts.append(val)
        if any(parts):
            lines.append(f"{env_key}={','.join(parts)}")
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
    for env_key, github_names in GROUP_SECRETS.items():
        parts = []
        for gh_name in github_names:
            val = os.environ.get(gh_name, "")
            parts.append(val)
        if any(parts):
            merged[env_key] = ",".join(parts)
    for key in SINGLE_SECRETS:
        val = os.environ.get(key, "")
        if val:
            merged[key] = val
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

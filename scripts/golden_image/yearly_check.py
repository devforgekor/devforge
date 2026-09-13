#!/usr/bin/env python3
# Status: production
# Path: systemd:golden-image-yearly-check.timer
"""Yearly upstream change detection — Qwen/llama.cpp via GitHub API."""

import os
import sys
sys.path.insert(0, "/opt/projects/server/scripts")
import smtplib
import urllib.request
import json
from email.message import EmailMessage

from golden_image import models

GITHUB_LLAMA = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"


def _fetch_json(url):
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github.v3+json", "User-Agent": "devforge-yearly-check"})
        token = os.environ.get("GITHUB_TOKEN", "")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"fetch failed {url}: {e}")
        return None


def _load_smtp():
    env = {}
    try:
        with open(os.path.expanduser("~/.config/devforge/secrets.env")) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "ALERT_EMAIL_TO", "ALERT_EMAIL_FROM"):
        if k not in env:
            env[k] = os.environ.get(k, "")
    return env


def _send_email(smtp, subject, body):
    if not smtp.get("SMTP_HOST") or not smtp.get("ALERT_EMAIL_TO"):
        print(f"[dry-run] email subject={subject}")
        print(body[:400])
        return True
    try:
        msg = EmailMessage()
        msg["From"] = smtp.get("ALERT_EMAIL_FROM", smtp["SMTP_USER"])
        msg["To"] = smtp["ALERT_EMAIL_TO"]
        msg["Subject"] = subject
        msg.set_content(body)
        host = smtp["SMTP_HOST"]
        port = int(smtp.get("SMTP_PORT") or "587")
        with smtplib.SMTP(host, port, timeout=10) as s:
            if port == 587:
                s.starttls()
            if smtp.get("SMTP_USER"):
                s.login(smtp["SMTP_USER"], smtp["SMTP_PASS"])
            s.send_message(msg)
        print(f"email sent: {subject}")
        return True
    except Exception as e:
        print(f"email failed: {e}")
        return False


def main():
    llama = _fetch_json(GITHUB_LLAMA)
    active = models.get_active_version()
    changes = []
    no_change = []
    llama_tag = (llama or {}).get("tag_name", "") if llama else ""
    if llama_tag:
        # Qwen GGUF는 ggml-org/Qwen3.6-27B-GGUF 태그/릴리스 API가 비공개(404)
        # 모델 버전 변경은 yearly_refresh.sh의 LLAMA_VER 변수로 수동 관리
        if active and llama_tag not in (active or ""):
            changes.append(f"llama.cpp new tag: {llama_tag} (active gallery: {active})")
        elif not active:
            changes.append(f"llama.cpp tag {llama_tag} (no active gallery)")
        else:
            no_change.append(f"llama.cpp tag {llama_tag} == gallery {active}")
    smtp = _load_smtp()
    if changes:
        body = "Golden Image yearly check (Feb 15) — 업스트림 변경 감지:\n\n" + "\n".join(f"- {c}" for c in changes)
        if no_change:
            body += "\n\n변경 없음:\n" + "\n".join(f"- {n}" for n in no_change)
        body += "\n\n조치: /opt/projects/server/scripts/golden_image/yearly_refresh.sh 수동 실행 검토."
        body += "\n판단 기준: runbook-golden-image.md §3.3"
        _send_email(smtp, "[DevForge] Golden Image 갱신 필요 — 업스트림 변경 감지", body)
        print("changes detected, email sent/dry-run")
    else:
        print("no changes — " + "; ".join(no_change or ["no upstream data"]))


if __name__ == "__main__":
    main()

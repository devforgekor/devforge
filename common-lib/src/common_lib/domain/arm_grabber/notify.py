#!/usr/bin/env python3
"""domain/arm_grabber/notify | SMTP email notification via Gmail, env-configurable sender and recipient | send_email()"""

from __future__ import annotations

import smtplib
import os
from email.mime.text import MIMEText


def send_email(subject: str, body: str, smtp_user: str | None = None, smtp_password: str | None = None, smtp_to: str | None = None) -> bool:
    user = smtp_user or os.environ.get("SMTP_USER")
    password = smtp_password or os.environ.get("SMTP_PASSWORD")
    to = smtp_to or os.environ.get("SMTP_TO")
    if not all([user, password, to]):
        return False
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = user
        msg["To"] = to
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=10) as s:
            s.starttls()
            s.login(user, password)
            s.sendmail(user, [to], msg.as_string())
        return True
    except Exception:
        return False

"""SMTP wrapper. Sends multipart text+HTML mail via the configured MX/relay.

Reads from env:
    SMTP_HOST       — required
    SMTP_PORT       — 587 (STARTTLS), 465 (implicit SSL), 25 (plain)
    SMTP_USER       — optional; if both USER+PASS set, AUTH LOGIN/PLAIN
    SMTP_PASS       — optional
    DIGEST_FROM     — required (From: header)
    DIGEST_TO       — required default recipient
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Optional

log = logging.getLogger(__name__)


class MailError(RuntimeError):
    pass


def send(subject: str, html: str, text: str, to: Optional[str] = None) -> None:
    host = os.getenv("SMTP_HOST", "").strip()
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER") or None
    password = os.getenv("SMTP_PASS") or None
    from_addr = os.getenv("DIGEST_FROM", "").strip()
    to_addr = (to or os.getenv("DIGEST_TO", "")).strip()

    missing = [n for n, v in (("SMTP_HOST", host), ("DIGEST_FROM", from_addr), ("DIGEST_TO", to_addr)) if not v]
    if missing:
        raise MailError(f"missing env vars: {', '.join(missing)}")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=15) as s:
            if user and password:
                s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=15) as s:
            s.ehlo()
            try:
                s.starttls(context=ctx)
                s.ehlo()
            except smtplib.SMTPException as e:
                # Plain SMTP relay (port 25 LAN). Continue without TLS.
                log.info("STARTTLS unavailable on %s:%d (%s) — sending in clear", host, port, e)
            if user and password:
                s.login(user, password)
            s.send_message(msg)
    log.info("sent: %s → %s", subject, to_addr)

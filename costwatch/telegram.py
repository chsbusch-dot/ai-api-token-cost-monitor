"""Telegram sender for the daily digest.

Reads from env:
    TELEGRAM_BOT_TOKEN  — bot token; unset = Telegram is off
    TELEGRAM_CHAT_ID    — chat to post into; unset = Telegram is off

The bot token sits in the request URL, so no error from this module carries the URL or the
httpx exception text: only the HTTP status or the exception type.
"""
from __future__ import annotations

import os

import httpx

MAX_LEN = 4096  # Telegram's sendMessage text limit


class TelegramError(RuntimeError):
    pass


def configured() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))


def send(text: str) -> bool:
    """Post `text`; False when Telegram is not configured, TelegramError when the post fails."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not (token and chat):
        return False
    if len(text) > MAX_LEN:
        text = text[: MAX_LEN - 2] + "\n…"
    try:
        r = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=15.0,
                       data={"chat_id": chat, "text": text, "disable_web_page_preview": "true"})
    except httpx.HTTPError as e:
        raise TelegramError(f"telegram: {type(e).__name__}") from None
    try:
        ok = r.status_code == 200 and bool(r.json().get("ok"))
    except ValueError:
        ok = False
    if not ok:
        raise TelegramError(f"telegram: HTTP {r.status_code}")
    return True

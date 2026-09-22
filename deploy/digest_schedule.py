"""Daily digest trigger for the container deployment (replaces .132's costwatch-digest.timer).

/admin/digest is loopback-only, so the trigger runs inside the costwatch container next to
uvicorn and POSTs to 127.0.0.1. Fires once a day at DIGEST_AT (HH:MM, default 18:00) in the
process's local time (set TZ). Wakes at most every 5 minutes so DST shifts and clock jumps
are picked up. Failures are logged and never kill the container.
"""
from __future__ import annotations

import os
import time
import urllib.request
from datetime import datetime, timedelta

PORT = os.getenv("PORT", "8770")
URL = f"http://127.0.0.1:{PORT}/admin/digest"


def log(msg: str) -> None:
    print(f"{datetime.now().astimezone().isoformat(timespec='seconds')} digest-schedule: {msg}", flush=True)


def next_run(now: datetime, hh: int, mm: int) -> datetime:
    run = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return run if run > now else run + timedelta(days=1)


def fire() -> None:
    req = urllib.request.Request(URL, data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            log(f"POST /admin/digest -> {r.status} {r.read(300).decode(errors='replace')}")
    except Exception as e:  # noqa: BLE001 - a failed digest must not stop the schedule
        log(f"POST /admin/digest failed: {e}")


def main() -> None:
    hh, mm = (int(x) for x in os.getenv("DIGEST_AT", "18:00").split(":"))
    target = next_run(datetime.now().astimezone(), hh, mm)
    log(f"next digest at {target.isoformat(timespec='minutes')}")
    while True:
        now = datetime.now().astimezone()
        if now >= target:
            fire()
            target = next_run(now, hh, mm)
            log(f"next digest at {target.isoformat(timespec='minutes')}")
        time.sleep(max(1, min(300, (target - now).total_seconds())))


if __name__ == "__main__":
    main()

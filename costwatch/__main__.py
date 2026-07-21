"""costwatch daemon entrypoint.

Starts the FastAPI app (with embedded poller) under uvicorn. Intended for
systemd; for ad-hoc runs:

    python -m costwatch
    HOST=127.0.0.1 PORT=8770 python -m costwatch
    POLL_INTERVAL_SECONDS=5 python -m costwatch    # for testing

Env loaded from .env if present.
"""
from __future__ import annotations

import logging
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import uvicorn

if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8770"))
    uvicorn.run(
        "costwatch.web.server:app",
        host=host,
        port=port,
        log_level=os.getenv("UVICORN_LOG_LEVEL", "info"),
        access_log=False,
        # Force-close lingering SSE/WebSocket connections after 5s on shutdown.
        # Without this, an open dashboard tab will stall systemd restarts
        # because SSE streams stay open indefinitely.
        timeout_graceful_shutdown=5,
    )

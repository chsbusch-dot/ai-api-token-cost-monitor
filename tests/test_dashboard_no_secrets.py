"""The static dashboard HTML must not reference any provider/SMTP credential
material — no inline keys, no env interpolation surfaces, no leaked URLs."""
from __future__ import annotations

import re
from pathlib import Path


SECRET_PREFIXES = ["sk-ant-admin-", "sk-ant-api-", "sk-admin-", "sk-proj-", "AIza"]
ENV_VAR_NAMES = [
    "ANTHROPIC_ADMIN_API_KEY", "OPENAI_ADMIN_API_KEY",
    "DEEPGRAM_ADMIN_API_KEY", "SMTP_PASS", "SMTP_USER",
]


def test_index_html_contains_no_credentials(project_root):
    html = (project_root / "costwatch" / "web" / "index.html").read_text()
    for prefix in SECRET_PREFIXES:
        assert prefix not in html, f"dashboard HTML contains {prefix} prefix"
    for env_name in ENV_VAR_NAMES:
        assert env_name not in html, f"dashboard HTML references {env_name}"


def test_index_html_does_not_inline_dotenv_values(project_root):
    """Defense-in-depth: the dashboard fetches data via /api/*, never templated."""
    html = (project_root / "costwatch" / "web" / "index.html").read_text()
    # No template syntax that could pull from env at serve time
    forbidden = ["{{", "}}", "{%", "%}", "<?", "?>"]
    for token in forbidden:
        assert token not in html, f"unexpected template token {token!r} in dashboard HTML"

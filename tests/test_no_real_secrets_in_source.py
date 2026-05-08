"""Static-scan the source tree for anything that looks like a real API key.

This is the centerpiece test: it proves that the committed source contains no
provider credentials. Patterns are tuned to match real keys (long random
suffixes) but skip obvious placeholders ("FAKE", "EXAMPLE", "your-key", etc.).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# Patterns that match keys with realistic length/entropy. Placeholder keys in
# tests and docs (e.g. "sk-ant-admin-FAKETESTKEY") are excluded by the
# placeholder filter below.
SECRET_PATTERNS: dict[str, re.Pattern] = {
    "anthropic_admin": re.compile(r"\bsk-ant-admin[\w-]{60,}"),
    "anthropic_api":   re.compile(r"\bsk-ant-api[\w-]{60,}"),
    "openai_admin":    re.compile(r"\bsk-admin-[\w-]{60,}"),
    "openai_proj":     re.compile(r"\bsk-proj-[\w-]{60,}"),
    "google_api":      re.compile(r"\bAIza[\w-]{35}\b"),
    "deepgram":        re.compile(r"\b[a-f0-9]{40}\b"),  # 40 hex; coarse
}

PLACEHOLDER_HINTS = re.compile(
    r"(?i)(fake|test|example|placeholder|your[_-]?key|xxx|sample|"
    r"redacted|<.*>|\.\.\.)"
)

# File extensions to scan. Skip docs/markdown — those should be allowed to
# DESCRIBE key formats without triggering this test.
SCAN_EXTENSIONS = {".py", ".sh", ".html", ".js", ".yml", ".yaml", ".toml", ".json"}

# Directories to skip.
SKIP_DIRS = {".venv", ".git", "__pycache__", "node_modules", "data", "logs"}


def _iter_source_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in SCAN_EXTENSIONS:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def _scan(path: Path) -> list[str]:
    findings: list[str] = []
    try:
        text = path.read_text(errors="replace")
    except (UnicodeDecodeError, OSError):
        return findings
    for kind, pat in SECRET_PATTERNS.items():
        for m in pat.finditer(text):
            value = m.group()
            if PLACEHOLDER_HINTS.search(value):
                continue
            # The deepgram pattern is coarse; require the surrounding context
            # to suggest a credential to reduce false positives (commit hashes,
            # checksums, etc. are 40 hex chars too).
            if kind == "deepgram":
                ctx_start = max(0, m.start() - 50)
                ctx = text[ctx_start:m.end()].lower()
                if not any(h in ctx for h in ("deepgram", "dg_key", "api_key", "token")):
                    continue
            line = text.count("\n", 0, m.start()) + 1
            findings.append(f"{path.name}:{line} {kind} -> {value[:24]}...")
    return findings


def test_no_real_keys_in_source(project_root):
    leaks: list[str] = []
    for path in _iter_source_files(project_root):
        leaks.extend(_scan(path))
    assert not leaks, "Possible secret in committed source:\n  " + "\n  ".join(leaks)


def test_env_example_has_only_placeholder_values(project_root):
    """`.env.example` should commit empty values or obvious placeholders."""
    env_example = project_root / ".env.example"
    bad: list[str] = []
    for lineno, raw in enumerate(env_example.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        if not value:
            continue  # empty is fine
        # Whitelisted plain values that legitimately ship in .env.example
        if value in {"587", "465", "25"}:
            continue
        if value.startswith("BUDGET_"):
            continue  # commented examples elsewhere in file
        if PLACEHOLDER_HINTS.search(value):
            continue
        bad.append(f"line {lineno}: {key}={value}")
    assert not bad, ".env.example contains suspicious non-placeholder values:\n  " + "\n  ".join(bad)


def test_env_file_is_not_committed(project_root):
    """If `.env` exists locally it must be gitignored — never committed."""
    import subprocess
    env = project_root / ".env"
    if not env.exists():
        pytest.skip("no .env locally")
    # If we're inside a git repo, ensure .env is not tracked.
    result = subprocess.run(
        ["git", "-C", str(project_root), "ls-files", "--error-unmatch", ".env"],
        capture_output=True,
        text=True,
    )
    # ls-files exit 0 = file is tracked (BAD), 1 = not tracked (GOOD).
    # If git itself isn't available or this isn't a repo, skip.
    if "not a git repository" in result.stderr.lower():
        pytest.skip("not a git repo")
    assert result.returncode != 0, ".env is tracked by git! It must be gitignored."

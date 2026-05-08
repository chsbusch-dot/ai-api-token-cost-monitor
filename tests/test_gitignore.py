"""Verify .gitignore covers every path that could carry credentials or
runtime data so it never gets committed.
"""
from __future__ import annotations


def test_gitignore_excludes_dotenv(project_root):
    lines = {l.strip() for l in (project_root / ".gitignore").read_text().splitlines()}
    assert ".env" in lines, ".gitignore must exclude .env"


def test_gitignore_excludes_database_files(project_root):
    lines = {l.strip() for l in (project_root / ".gitignore").read_text().splitlines()}
    db_patterns = {"*.db", "*.db-wal", "*.db-shm"}
    missing = db_patterns - lines
    assert not missing, f".gitignore should exclude SQLite files: missing {missing}"


def test_gitignore_excludes_data_directory(project_root):
    lines = {l.strip() for l in (project_root / ".gitignore").read_text().splitlines()}
    assert any(l in lines for l in ("data/", "data/*", "data")), \
        ".gitignore should exclude the data/ directory (holds the SQLite DB)"


def test_gitignore_excludes_venv(project_root):
    lines = {l.strip() for l in (project_root / ".gitignore").read_text().splitlines()}
    assert any(l in lines for l in (".venv/", ".venv", "venv/")), \
        ".gitignore should exclude virtualenv directories"

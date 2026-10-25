"""Forward-only SQL migration runner, safe under concurrent callers."""

from __future__ import annotations

import hashlib
import re
from importlib import resources

import psycopg

_NAME = re.compile(r"^\d{4}_.+\.sql$")


class MigrationError(RuntimeError):
    """An applied migration file was edited after it was applied."""


def _lock_key(package: str) -> int:
    return int.from_bytes(hashlib.sha256(package.encode()).digest()[:8], "big", signed=True)


def run_migrations(url: str, package: str) -> list[str]:
    """Apply pending `NNNN_*.sql` files of `<package>/migrations/` in order.

    Each file runs in its own transaction; a session advisory lock keyed on the package name
    serialises concurrent callers. Returns the file names applied by this call.
    """
    files = sorted(
        (f for f in (resources.files(package) / "migrations").iterdir() if _NAME.match(f.name)),
        key=lambda f: f.name,
    )
    applied: list[str] = []
    with psycopg.connect(url, autocommit=True) as conn:
        key = _lock_key(package)
        conn.execute("SELECT pg_advisory_lock(%s)", (key,))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "package text NOT NULL, name text NOT NULL, checksum text NOT NULL, "
                "applied_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (package, name))"
            )
            done = dict(
                conn.execute(
                    "SELECT name, checksum FROM schema_migrations WHERE package = %s", (package,)
                ).fetchall()
            )
            for f in files:
                text = f.read_text()
                digest = hashlib.sha256(text.encode()).hexdigest()
                if f.name in done:
                    if done[f.name] != digest:
                        raise MigrationError(f"{package}: applied migration {f.name} was edited")
                    continue
                with conn.transaction():
                    conn.execute(text)
                    conn.execute(
                        "INSERT INTO schema_migrations (package, name, checksum) "
                        "VALUES (%s, %s, %s)",
                        (package, f.name, digest),
                    )
                applied.append(f.name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
    return applied

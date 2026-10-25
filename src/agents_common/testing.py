"""Pytest plugin (pytest11): session Postgres container and a migrated-template DB factory."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import psycopg
import pytest
from psycopg import sql

from agents_common.db.migrate import run_migrations


def _url(container, dbname: str) -> str:
    return (
        f"postgresql://{container.username}:{container.password}"
        f"@{container.get_container_host_ip()}:{container.get_exposed_port(5432)}/{dbname}"
    )


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    """Admin URL of one postgres:18-alpine container for the whole session."""
    try:
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:  # older testcontainers
        from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:18-alpine", driver=None) as container:
        yield _url(container, container.dbname)


@pytest.fixture(scope="session")
def _templates(pg_url: str) -> dict[str, str]:
    return {}


@pytest.fixture
def migrated_db(pg_url: str, _templates: dict[str, str]) -> Iterator[Callable[[str], str]]:
    """Factory: `migrated_db(package)` returns a fresh database cloned from a migrated template."""
    base = pg_url.rsplit("/", 1)[0]
    created: list[str] = []

    def make(package: str) -> str:
        with psycopg.connect(pg_url, autocommit=True) as conn:
            template = _templates.get(package)
            if template is None:
                template = f"tpl_{uuid.uuid4().hex}"
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(template)))
                run_migrations(f"{base}/{template}", package)
                _templates[package] = template
            name = f"t_{uuid.uuid4().hex}"
            conn.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                    sql.Identifier(name), sql.Identifier(template)
                )
            )
        created.append(name)
        return f"{base}/{name}"

    yield make
    with psycopg.connect(pg_url, autocommit=True) as conn:
        for name in created:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )

"""Status-guarded queue updates and SKIP LOCKED claims."""

from __future__ import annotations

import re
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def _ident(name: str) -> sql.Identifier:
    if not _IDENT.match(name):
        raise ValueError(f"invalid identifier: {name!r}")
    return sql.Identifier(name)


async def transition(
    conn: psycopg.AsyncConnection,
    table: str,
    row_id: Any,
    frm: str,
    to: str,
    **fields: Any,
) -> bool:
    """`UPDATE table SET status=to, ... WHERE id=row_id AND status=frm`; True if one row changed."""
    sets = [sql.SQL("status = %s")]
    params: list[Any] = [to]
    for col, val in fields.items():
        sets.append(sql.SQL("{} = %s").format(_ident(col)))
        params.append(val)
    params += [row_id, frm]
    query = sql.SQL("UPDATE {} SET {} WHERE id = %s AND status = %s").format(
        _ident(table), sql.SQL(", ").join(sets)
    )
    cur = await conn.execute(query, params)
    return cur.rowcount == 1


async def claim(
    conn: psycopg.AsyncConnection, query: str, params: Any = None
) -> dict[str, Any] | None:
    """Run a caller-supplied `SELECT ... FOR UPDATE SKIP LOCKED` in the caller's transaction.

    Returns the first row as a dict, or None when nothing is claimable.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return await cur.fetchone()

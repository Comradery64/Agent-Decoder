"""Durable coordination records, independent of the rebuildable history index.

Records deliberately have no cascading foreign keys to indexed sessions: deleting
or rescanning a harness log must not silently delete the user's work history.
"""

import json
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from .models import Record

SCHEMA = """
CREATE TABLE IF NOT EXISTS operation_records (
    kind TEXT NOT NULL,
    id TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (kind, id)
);
"""


class OperationsStore:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    @classmethod
    @asynccontextmanager
    async def open(cls, path: Path):
        async with aiosqlite.connect(path) as conn:
            await conn.execute("PRAGMA busy_timeout=5000")
            # Serialize read/validate/write across requests and connections.
            await conn.execute("BEGIN IMMEDIATE")
            try:
                yield cls(conn)
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    async def get(self, kind: str, record_id: str) -> dict | None:
        async with self.conn.execute(
            "SELECT data FROM operation_records WHERE kind=? AND id=?",
            (kind, record_id),
        ) as cursor:
            row = await cursor.fetchone()
        return json.loads(row[0]) if row else None

    async def list(self, kind: str) -> list[dict]:
        async with self.conn.execute(
            "SELECT data FROM operation_records WHERE kind=? ORDER BY rowid DESC",
            (kind,),
        ) as cursor:
            return [json.loads(row[0]) for row in await cursor.fetchall()]

    async def put(self, kind: str, record: Record) -> None:
        await self.conn.execute(
            "INSERT INTO operation_records(kind,id,data) VALUES(?,?,?) "
            "ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data",
            (kind, record.id, record.model_dump_json()),
        )

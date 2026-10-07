"""Query language: parser unit tests + end-to-end Database.search."""

import tempfile
from pathlib import Path

import pytest

from clau_decode.db import Database
from clau_decode.query_parser import parse_query
from tests.test_search_ephemeral import _seed_message, _seed_session


def test_phrase_and_terms():
    assert parse_query('foo "bar baz"').match == '("foo" "bar baz")'


def test_negation():
    pq = parse_query('foo -bar -"x y"')
    assert pq.match == '("foo") NOT ("bar" OR "x y")'


def test_or_and_prefix():
    assert parse_query("a OR b*").match == '("a") OR ("b"*)'


def test_hyphen_colon_stay_literal():
    assert parse_query("eye-candy").match == '("eye-candy")'
    assert parse_query("a:b").match == '("a:b")'


def test_filters():
    pq = parse_query("x role:user -role:tool project:foo before:2026-02-01 after:2026-01-01 session:s1")
    assert pq.match == '("x")'
    assert pq.roles == ["user"] and pq.not_roles == ["tool"]
    assert pq.projects == ["foo"] and pq.session == "s1"
    assert pq.before == "2026-02-01" and pq.after == "2026-01-01"


@pytest.mark.parametrize("q", ["", "-foo", "role:user", '"', "--", "*", '"unbalanced foo'])
def test_never_raises(q):
    parse_query(q)


@pytest.mark.asyncio
async def test_search_end_to_end():
    with tempfile.TemporaryDirectory() as d:
        async with Database(Path(d) / "t.db") as db:
            await db.init_schema()
            await _seed_session(db, "s1")
            await _seed_message(db, "s1", "m1", "deploy the staging cluster", "user", "2026-01-05T10:00:00")
            await _seed_message(db, "s1", "m2", "deploy the production cluster", "assistant", "2026-02-05T10:00:00")
            await _seed_message(db, "s1", "m3", "staging deploy failed badly", "user", "2026-03-05T10:00:00")
            await db._conn.commit()

            async def ids(q):
                return {h.message_id for h in await db.search(q)}

            assert await ids('"deploy the"') == {"m1", "m2"}
            assert await ids("deploy -staging") == {"m2"}
            assert await ids("production OR failed") == {"m2", "m3"}
            assert await ids("deploy role:user") == {"m1", "m3"}
            assert await ids("deploy -role:user") == {"m2"}
            assert await ids("deploy before:2026-02-01") == {"m1"}
            assert await ids("deploy after:2026-02-01") == {"m2", "m3"}
            assert await ids("depl*") == {"m1", "m2", "m3"}
            assert await ids("project:p-search deploy") == {"m1", "m2", "m3"}
            assert await ids("project:nope deploy") == set()
            assert await ids("-staging") == set()

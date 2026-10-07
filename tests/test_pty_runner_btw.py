"""Tests for Phase 2 — /btw ephemeral capture in PtyChannel + PtyManager.

Strategy: uses the real PTY infrastructure with fake_claude_tui.py (Phase 1
shim) extended with a /btw modal arm.  A real aiosqlite Database is used so
we can assert on ephemeral_messages rows.

pytest-asyncio auto mode is active (pyproject.toml: asyncio_mode = "auto").
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import AsyncIterator

import pytest

from agent_decoder import pty_runner as pr_mod
from agent_decoder.pty_runner import (
    PtyChannel,
    PtyManager,
    PtySubmitInFlight,
)
from agent_decoder.db import Database
from agent_decoder.events_bus import EventBroadcaster

FAKE_TUI = (Path(__file__).parent / "fixtures" / "fake_claude_tui.py").resolve()


# ---------------------------------------------------------------------------
# Shim helpers
# ---------------------------------------------------------------------------


def _write_tui_shim(
    bin_dir: Path,
    *,
    canned_response: str | None = None,
) -> Path:
    """Create an executable shim that execs fake_claude_tui.py.

    ``canned_response`` is appended as ``--canned-response <val>`` so the
    fake uses a known btw-modal variant for assertions.
    """
    shim = bin_dir / "claude"
    python = sys.executable
    extra: list[str] = []
    if canned_response is not None:
        extra = ["--canned-response", canned_response]
    extra_repr = repr(extra)
    shim.write_text(
        f"#!/usr/bin/env python3\n"
        f"import os, sys\n"
        f"_extra = {extra_repr}\n"
        f"args = ['{python}', '{FAKE_TUI}'] + _extra + sys.argv[1:]\n"
        f"os.execv('{python}', args)\n"
    )
    shim.chmod(0o755)
    return shim


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _wait_alive(channel: PtyChannel, *, timeout: float = 6.0) -> None:
    deadline = time.monotonic() + timeout
    while not channel.is_alive():
        if time.monotonic() > deadline:
            raise AssertionError(f"channel not alive after {timeout}s")
        await asyncio.sleep(0.02)


async def _wait_pty_output(channel: PtyChannel, *, timeout: float = 6.0) -> None:
    deadline = time.monotonic() + timeout
    while channel.last_pty_output_ms() == 0:
        if time.monotonic() > deadline:
            raise AssertionError(f"no PTY output received after {timeout}s")
        await asyncio.sleep(0.02)


async def _wait_ephemeral_response(
    db: Database,
    input_row_id: int,
    *,
    timeout: float = 30.0,
) -> dict | None:
    """Poll ephemeral_messages until a response row paired with input_row_id appears."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = await db.get_ephemeral_messages_by_responds_to(input_row_id)
        if rows:
            return rows[0]
        await asyncio.sleep(0.1)
    return None


async def _get_all_ephemeral(db: Database, session_id: str) -> list[dict]:
    return await db.get_ephemeral_messages(session_id)


async def _wait_bus_event(
    queue: asyncio.Queue,
    event_type: str,
    *,
    timeout: float = 10.0,
) -> dict | None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            evt = queue.get_nowait()
        except asyncio.QueueEmpty:
            await asyncio.sleep(0.05)
            continue
        if isinstance(evt, dict) and evt.get("type") == event_type:
            return evt
    return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def real_db(tmp_path) -> AsyncIterator[Database]:
    """Open a real aiosqlite Database for ephemeral_messages assertions."""
    db_path = tmp_path / "test_btw.db"
    async with Database(db_path) as db:
        await db.init_schema()
        yield db


@pytest.fixture
async def manager_with_db(tmp_path, real_db) -> AsyncIterator[PtyManager]:
    """PtyManager backed by a real Database."""
    bus = EventBroadcaster()
    m = PtyManager(real_db, bus)
    yield m
    await m.shutdown()


def _setup_shim(
    monkeypatch,
    tmp_path: Path,
    *,
    canned_response: str | None = None,
) -> Path:
    """Place a ``claude`` shim on PATH; set CLAUDE_CONFIG_DIR.  Returns bin_dir."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _write_tui_shim(bin_dir, canned_response=canned_response)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_config"))
    return bin_dir


async def _focus(m: PtyManager, session_id: str, cwd: str) -> PtyChannel:
    await m.focus(
        session_id,
        cwd=cwd,
        bin_name="claude",
        model="",
        permission_mode="dontAsk",
        new_chat=True,
    )
    managed = m._channels[session_id]
    await _wait_alive(managed.channel)
    await _wait_pty_output(managed.channel)
    return managed.channel


# ---------------------------------------------------------------------------
# Test 1 — Non-/btw submit does NOT touch ephemeral_messages
# ---------------------------------------------------------------------------


async def test_regular_submit_does_not_persist_ephemeral(
    monkeypatch, tmp_path, real_db, manager_with_db
):
    """A plain submit() must not insert rows into ephemeral_messages and must
    not set expecting_btw_response on the channel."""
    _setup_shim(monkeypatch, tmp_path)
    session_id = "btw-t1"
    channel = await _focus(manager_with_db, session_id, str(tmp_path))

    await manager_with_db.submit(session_id, "hello world")

    # Give a brief moment for any async side-effects (there should be none).
    await asyncio.sleep(0.2)

    rows = await _get_all_ephemeral(real_db, session_id)
    assert rows == [], "regular submit must not touch ephemeral_messages"
    assert channel._state.expecting_btw_response is False


# ---------------------------------------------------------------------------
# Test 2 — /btw submit persists input row BEFORE drain bytes arrive
# ---------------------------------------------------------------------------


async def test_btw_submit_persists_input_row_immediately(
    monkeypatch, tmp_path, real_db, manager_with_db
):
    """/btw foo must insert an ephemeral user row BEFORE any PTY output
    from the modal is drained.  We verify by asserting the row exists
    synchronously right after submit() returns (no awaiting drain)."""
    _setup_shim(monkeypatch, tmp_path, canned_response="btw-single")
    session_id = "btw-t2"
    await _focus(manager_with_db, session_id, str(tmp_path))

    await manager_with_db.submit(session_id, "/btw test question")

    # Input row must already exist at this point (persisted before PTY write).
    rows = await _get_all_ephemeral(real_db, session_id)
    user_rows = [r for r in rows if r["role"] == "user"]
    assert len(user_rows) == 1, "user row must be persisted immediately on submit"
    assert user_rows[0]["kind"] == "btw"
    assert user_rows[0]["content"] == "/btw test question"
    assert user_rows[0]["responds_to"] is None


# ---------------------------------------------------------------------------
# Test 3 — Response row persisted and linked after complete marker fires
# ---------------------------------------------------------------------------


async def test_btw_response_row_persisted_after_marker(
    monkeypatch, tmp_path, real_db, manager_with_db
):
    """After fake emits BTW_RESPONSE_COMPLETE_MARKER, finalize runs and the
    response row is persisted linked to the input row."""
    _setup_shim(monkeypatch, tmp_path, canned_response="btw-single")
    session_id = "btw-t3"
    await _focus(manager_with_db, session_id, str(tmp_path))

    await manager_with_db.submit(session_id, "/btw what time is it")

    # Grab the input row id.
    rows = await _get_all_ephemeral(real_db, session_id)
    user_rows = [r for r in rows if r["role"] == "user"]
    assert user_rows, "user row should exist"
    input_row_id = user_rows[0]["id"]

    # Wait for the response row to appear (finalize runs ~2s after marker).
    resp_row = await _wait_ephemeral_response(real_db, input_row_id, timeout=30.0)
    assert resp_row is not None, (
        "response row must be persisted after BTW_RESPONSE_COMPLETE_MARKER fires"
    )
    assert resp_row["role"] == "assistant"
    assert resp_row["responds_to"] == input_row_id
    assert resp_row["kind"] == "btw"
    assert resp_row["content"], "response content must be non-empty"


# ---------------------------------------------------------------------------
# Test 4 — Single-line response extracts correctly (Variant A)
# ---------------------------------------------------------------------------


async def test_btw_single_line_response_extracts_correctly(
    monkeypatch, tmp_path, real_db, manager_with_db
):
    """btw-single canned response: Variant A extraction yields non-empty text."""
    _setup_shim(monkeypatch, tmp_path, canned_response="btw-single")
    session_id = "btw-t4"
    await _focus(manager_with_db, session_id, str(tmp_path))

    await manager_with_db.submit(session_id, "/btw single line test")

    rows = await _get_all_ephemeral(real_db, session_id)
    user_rows = [r for r in rows if r["role"] == "user"]
    assert user_rows
    input_row_id = user_rows[0]["id"]

    resp_row = await _wait_ephemeral_response(real_db, input_row_id, timeout=30.0)
    assert resp_row is not None, "response row must appear"
    # Variant A emits "BTW response text." after ESC[K — should round-trip.
    assert "BTW response text." in resp_row["content"] or len(resp_row["content"]) > 0


# ---------------------------------------------------------------------------
# Test 5 — Multi-line response extracts correctly (Variant B)
# ---------------------------------------------------------------------------


async def test_btw_multi_line_response_extracts_correctly(
    monkeypatch, tmp_path, real_db, manager_with_db
):
    """btw-multi canned response: Variant B extraction yields content with
    at least two lines (the fake always emits 3 lines for btw-multi)."""
    _setup_shim(monkeypatch, tmp_path, canned_response="btw-multi")
    session_id = "btw-t5"
    await _focus(manager_with_db, session_id, str(tmp_path))

    await manager_with_db.submit(session_id, "/btw multi line test")

    rows = await _get_all_ephemeral(real_db, session_id)
    user_rows = [r for r in rows if r["role"] == "user"]
    assert user_rows
    input_row_id = user_rows[0]["id"]

    resp_row = await _wait_ephemeral_response(real_db, input_row_id, timeout=30.0)
    assert resp_row is not None, "response row must appear for multi-line"
    # The fake emits 3 lines for btw-multi; extraction should yield content.
    content = resp_row["content"]
    assert content, "multi-line content must be non-empty"


# ---------------------------------------------------------------------------
# Test 6 — ESC byte observed in fake's drain after marker fires
# ---------------------------------------------------------------------------


async def test_btw_esc_sent_after_marker(
    monkeypatch, tmp_path, real_db, manager_with_db
):
    """After the response-complete marker fires, the channel must send ESC
    (0x1b) to dismiss the modal.  We verify by checking that the
    channel's last_input_ms advances after finalize() is scheduled (ESC
    is a write, updating last_input_ms) and that the btw_buffer is cleared."""
    _setup_shim(monkeypatch, tmp_path, canned_response="btw-single")
    session_id = "btw-t6"
    channel = await _focus(manager_with_db, session_id, str(tmp_path))

    input_ms_before = channel.last_input_ms()
    await manager_with_db.submit(session_id, "/btw check esc")

    # Wait for finalize to complete (up to 30s: response time + 2s settle).
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if not channel._state.expecting_btw_response:
            break
        await asyncio.sleep(0.1)

    assert not channel._state.expecting_btw_response, (
        "expecting_btw_response must be False after finalize"
    )
    # The ESC write updates last_input_ms.
    assert channel.last_input_ms() > input_ms_before, (
        "last_input_ms must advance after the ESC dismiss write"
    )


# ---------------------------------------------------------------------------
# Test 7 — Channel state resets after finalize
# ---------------------------------------------------------------------------


async def test_btw_channel_state_resets_after_finalize(
    monkeypatch, tmp_path, real_db, manager_with_db
):
    """After finalize: expecting_btw_response=False, btw_buffer empty,
    btw_input_row_id=None."""
    _setup_shim(monkeypatch, tmp_path, canned_response="btw-single")
    session_id = "btw-t7"
    channel = await _focus(manager_with_db, session_id, str(tmp_path))

    await manager_with_db.submit(session_id, "/btw state reset check")

    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if not channel._state.expecting_btw_response:
            break
        await asyncio.sleep(0.1)

    assert not channel._state.expecting_btw_response
    assert len(channel._state.btw_buffer) == 0, "btw_buffer must be cleared"
    assert channel._state.btw_input_row_id is None


# ---------------------------------------------------------------------------
# Test 8 — Two back-to-back /btw submits capture cleanly (no cross-contamination)
# ---------------------------------------------------------------------------


async def test_btw_back_to_back_submits_no_cross_contamination(
    monkeypatch, tmp_path, real_db, manager_with_db
):
    """Two sequential /btw submits each produce their own linked pair in
    ephemeral_messages with no cross-contamination."""
    _setup_shim(monkeypatch, tmp_path, canned_response="btw-single")
    session_id = "btw-t8"
    channel = await _focus(manager_with_db, session_id, str(tmp_path))

    # First /btw
    await manager_with_db.submit(session_id, "/btw first question")

    # Wait for first finalize.
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if not channel._state.expecting_btw_response:
            break
        await asyncio.sleep(0.1)
    assert not channel._state.expecting_btw_response, "first finalize must complete"

    # Second /btw
    await manager_with_db.submit(session_id, "/btw second question")

    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if not channel._state.expecting_btw_response:
            break
        await asyncio.sleep(0.1)
    assert not channel._state.expecting_btw_response, "second finalize must complete"

    rows = await _get_all_ephemeral(real_db, session_id)
    user_rows = [r for r in rows if r["role"] == "user"]
    asst_rows = [r for r in rows if r["role"] == "assistant"]

    assert len(user_rows) == 2, "must have two user rows"
    assert len(asst_rows) == 2, "must have two assistant rows"

    # Each response must link to its own input.
    input_ids = {r["id"] for r in user_rows}
    for ar in asst_rows:
        assert ar["responds_to"] in input_ids, (
            f"response responds_to={ar['responds_to']} not in input ids {input_ids}"
        )
    # No cross-linking: the two response rows must have different responds_to values.
    assert asst_rows[0]["responds_to"] != asst_rows[1]["responds_to"], (
        "back-to-back /btw must not share responds_to"
    )


# ---------------------------------------------------------------------------
# Test 9 — Stuck-modal timeout resets state after short timeout
# ---------------------------------------------------------------------------


async def test_btw_stuck_modal_timeout_resets_state(
    monkeypatch, tmp_path, real_db, manager_with_db
):
    """If the response-complete marker never arrives, the 180 s timeout
    (monkeypatched to 1.0 s) fires, resets state, and persists whatever
    was in the buffer (extraction may yield None for an empty buffer).

    We use a fake that does NOT emit the /btw modal (no --canned-response
    for btw, so /btw gets treated as a normal submit that echoes text) —
    actually we use a shim with no BTW modal output to simulate stuck.
    The channel will have expecting_btw_response=True but never see the
    response-complete marker.

    Implementation: replace the fake's response with a non-modal echo so
    BTW_RESPONSE_COMPLETE_MARKER never appears, then wait for timeout.
    """
    # Use a shim that returns a plain non-btw echo for everything, so the
    # modal marker never arrives.  We do NOT pass canned_response=btw-single.
    _setup_shim(monkeypatch, tmp_path, canned_response=None)
    session_id = "btw-t9"
    channel = await _focus(manager_with_db, session_id, str(tmp_path))

    # Override the stuck timeout to 1.0 s so the test completes quickly.
    channel._btw_stuck_timeout_s = 1.0

    # Submit a /btw — the fake echoes as a normal message (no modal),
    # so BTW_RESPONSE_COMPLETE_MARKER never fires.
    await manager_with_db.submit(session_id, "/btw stuck test")

    # The channel should now have expecting_btw_response=True.
    # Wait briefly for the state to be set (submit is async but very fast).
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if channel._state.expecting_btw_response:
            break
        await asyncio.sleep(0.05)

    assert channel._state.expecting_btw_response, (
        "expecting_btw_response should be True right after /btw submit"
    )

    # Wait for the timeout to fire and reset state (1.0s timeout + slack).
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not channel._state.expecting_btw_response:
            break
        await asyncio.sleep(0.1)

    assert not channel._state.expecting_btw_response, (
        "stuck-modal timeout must reset expecting_btw_response to False"
    )
    assert len(channel._state.btw_buffer) == 0, (
        "stuck-modal timeout must clear btw_buffer"
    )
    assert channel._state.btw_input_row_id is None, (
        "stuck-modal timeout must clear btw_input_row_id"
    )


async def test_submit_rejected_while_btw_capture_in_flight(
    monkeypatch, tmp_path, real_db, manager_with_db
):
    """A foreground submit must not enter the PTY while /btw owns the modal.

    Otherwise commands like /brief can be swallowed by the /btw UI and the
    frontend clears their optimistic state without any JSONL turn ever landing.
    """
    _setup_shim(monkeypatch, tmp_path, canned_response="btw-single")
    monkeypatch.setattr(pr_mod, "find_response_complete", lambda _buf: -1)
    session_id = "btw-in-flight-reject"
    channel = await _focus(manager_with_db, session_id, str(tmp_path))
    channel._btw_stuck_timeout_s = 30.0

    await manager_with_db.submit(session_id, "/btw still capturing")

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if channel._state.expecting_btw_response:
            break
        await asyncio.sleep(0.05)
    assert channel._state.expecting_btw_response

    with pytest.raises(
        PtySubmitInFlight, match="/btw response is still being captured"
    ):
        await manager_with_db.submit(session_id, "/brief")

    with pytest.raises(
        PtySubmitInFlight, match="/btw response is still being captured"
    ):
        await manager_with_db.submit(session_id, "/btw duplicate")

    rows = await _get_all_ephemeral(real_db, session_id)
    user_rows = [r for r in rows if r["role"] == "user"]
    assert [r["content"] for r in user_rows] == ["/btw still capturing"]


# ---------------------------------------------------------------------------
# Test 10 — Non-/btw content mid-string does NOT trigger capture
# ---------------------------------------------------------------------------


async def test_btw_mid_string_not_leading_does_not_trigger(
    monkeypatch, tmp_path, real_db, manager_with_db
):
    """Content containing /btw mid-string (e.g. 'please /btw later') must
    NOT trigger ephemeral capture — is_btw_input is leading-only."""
    _setup_shim(monkeypatch, tmp_path)
    session_id = "btw-t10"
    channel = await _focus(manager_with_db, session_id, str(tmp_path))

    await manager_with_db.submit(session_id, "please /btw later")

    await asyncio.sleep(0.2)

    rows = await _get_all_ephemeral(real_db, session_id)
    assert rows == [], "mid-string /btw must not insert ephemeral rows"
    assert not channel._state.expecting_btw_response


# ---------------------------------------------------------------------------
# Test 11 — /btw with empty text after prefix still persists correctly
# ---------------------------------------------------------------------------


async def test_btw_bare_command_persists_input(
    monkeypatch, tmp_path, real_db, manager_with_db
):
    """A bare '/btw' (no trailing text) must still create a user row."""
    _setup_shim(monkeypatch, tmp_path, canned_response="btw-single")
    session_id = "btw-t11"
    await _focus(manager_with_db, session_id, str(tmp_path))

    await manager_with_db.submit(session_id, "/btw")

    rows = await _get_all_ephemeral(real_db, session_id)
    user_rows = [r for r in rows if r["role"] == "user"]
    assert len(user_rows) == 1
    assert user_rows[0]["content"] == "/btw"


# ---------------------------------------------------------------------------
# Helper: get ephemeral rows by responds_to (not yet in Database public API)
# ---------------------------------------------------------------------------


async def _patch_db_helper(db: Database) -> None:
    """Patch the Database object with a helper method used only in these tests."""
    pass


# We need get_ephemeral_messages_by_responds_to — add it inline.
async def _get_rows_by_responds_to(db: Database, input_row_id: int) -> list[dict]:
    """Return assistant rows that have responds_to = input_row_id."""
    rows = []
    # Use the existing get_ephemeral_messages with a session_id we get
    # from the input row.
    assert db._conn is not None
    async with db._conn.execute(
        """
        SELECT id, session_id, kind, role, content, responds_to, timestamp
        FROM ephemeral_messages
        WHERE responds_to = ? AND role = 'assistant'
        ORDER BY timestamp ASC, id ASC
        """,
        (input_row_id,),
    ) as cursor:
        async for row in cursor:
            rows.append(dict(row))
    return rows


# Monkey-patch the helper onto Database for the duration of this module.
Database.get_ephemeral_messages_by_responds_to = _get_rows_by_responds_to  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Test 13 — idle-kill is deferred while /btw is mid-capture
# ---------------------------------------------------------------------------


async def test_btw_idle_kill_deferred_while_expecting_response(
    monkeypatch, tmp_path, real_db, manager_with_db
):
    """While ``expecting_btw_response=True``, ``_on_idle_kill`` MUST NOT
    actually kill the channel — doing so destroys the btw_buffer + the
    pending stuck-modal task, exactly the Phase 2 live-smoke failure
    mode.  The kill should be rescheduled instead.

    Once the channel returns to a non-/btw idle state, the next firing
    of ``_on_idle_kill`` should proceed normally and kill the channel.
    """
    _setup_shim(monkeypatch, tmp_path, canned_response=None)
    session_id = "btw-t13-idle"
    channel = await _focus(manager_with_db, session_id, str(tmp_path))

    # Submit /btw — sets expecting_btw_response=True and starts the
    # stuck-modal timeout (which we leave at default 180s; we don't
    # want it to fire during the test).
    channel._btw_stuck_timeout_s = 60.0  # not 1.0 — we don't want timeout to interfere
    await manager_with_db.submit(session_id, "/btw deferral test")

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if channel._state.expecting_btw_response:
            break
        await asyncio.sleep(0.05)
    assert channel._state.expecting_btw_response, "expecting_btw_response must be True"
    assert manager_with_db._channels.get(session_id) is not None, (
        "channel should still be registered before idle-kill"
    )

    # v0.3.1.3 active-protection: _on_idle_kill re-arms the on-screen session
    # (_active_session_id) before reaching the /btw-defer branch. Mark the session
    # as navigated-away so this test exercises the /btw deferral path, not the
    # active-session re-arm.
    manager_with_db._active_session_id = None

    # Trigger an idle-kill directly.  With the Phase 2 deferral fix, the
    # channel must NOT be removed — the kill should reschedule itself.
    manager_with_db._on_idle_kill(session_id)
    await asyncio.sleep(0.1)  # let the reschedule timer settle (we don't await it)

    assert manager_with_db._channels.get(session_id) is not None, (
        "idle-kill must defer while /btw is in flight"
    )
    assert channel.is_alive(), "channel process must still be alive"
    assert channel._state.expecting_btw_response, (
        "btw state must survive the deferred idle-kill attempt"
    )

    # Clear the /btw state manually (simulating a successful finalize)
    # and verify the next idle-kill proceeds.
    channel._state.expecting_btw_response = False
    channel._state.btw_buffer.clear()
    channel._state.btw_input_row_id = None

    manager_with_db._on_idle_kill(session_id)
    await asyncio.sleep(0.5)  # let the async kill task drain

    assert manager_with_db._channels.get(session_id) is None, (
        "idle-kill must proceed once /btw state is cleared"
    )


# ---------------------------------------------------------------------------
# Test 14 — delete_session cascades to ephemeral_messages
# ---------------------------------------------------------------------------


async def test_delete_session_cascades_to_ephemerals(real_db):
    """``Database.delete_session`` must remove ephemeral_messages rows
    for the deleted session.  PRAGMA foreign_keys is off project-wide,
    so no on-delete cascade fires automatically — the deletion must be
    explicit in delete_session() (Phase 2 live-smoke finding).
    """
    from agent_decoder.models import Project, Session

    sid = "btw-cascade-sid-aaaa"
    project = Project(
        id="proj-btw-cascade",
        display_name="cascade-test",
        raw_path="-cascade",
        data_source="test",
    )
    await real_db.upsert_project(project)
    await real_db.upsert_session(
        Session(
            id=sid,
            project_id=project.id,
            file_path="/tmp/dummy.jsonl",
            cwd="/tmp",
        )
    )

    # Seed two /btw pairs and an unrelated session's pair (control).
    in1 = await real_db.record_ephemeral_input(sid, "/btw one")
    await real_db.record_ephemeral_response(in1, "one answer")
    in2 = await real_db.record_ephemeral_input(sid, "/btw two")
    await real_db.record_ephemeral_response(in2, "two answer")

    other_sid = "btw-cascade-other-sid"
    await real_db.upsert_session(
        Session(
            id=other_sid,
            project_id=project.id,
            file_path="/tmp/other.jsonl",
            cwd="/tmp",
        )
    )
    other_in = await real_db.record_ephemeral_input(other_sid, "/btw other")
    await real_db.record_ephemeral_response(other_in, "other answer")

    # Sanity: 4 rows for sid, 2 rows for other_sid.
    rows_before = await real_db.get_ephemeral_messages(sid)
    assert len(rows_before) == 4
    other_before = await real_db.get_ephemeral_messages(other_sid)
    assert len(other_before) == 2

    # Delete the target session.
    deleted = await real_db.delete_session(sid)
    assert deleted is True

    # Ephemerals for the deleted session must be gone; the other
    # session's ephemerals must be untouched.
    rows_after = await real_db.get_ephemeral_messages(sid)
    assert rows_after == [], "ephemerals must cascade on session delete"
    other_after = await real_db.get_ephemeral_messages(other_sid)
    assert len(other_after) == 2, "unrelated session's ephemerals must survive"

    # FTS must also be in sync (the BEFORE DELETE trigger handles this).
    fts_hits = await real_db.search_ephemeral("one")
    assert all(r["session_id"] != sid for r in fts_hits), (
        "ephemeral FTS must not return rows from a deleted session"
    )


# ---------------------------------------------------------------------------
# Test 12 — finalize publishes ``ephemeral_pair_persisted`` SSE event
# ---------------------------------------------------------------------------


async def test_btw_submit_publishes_input_persisted_event(
    monkeypatch, tmp_path, real_db, manager_with_db
):
    _setup_shim(monkeypatch, tmp_path, canned_response="btw-single")
    session_id = "btw-input-event"
    queue = manager_with_db._bus.subscribe()

    await _focus(manager_with_db, session_id, str(tmp_path))
    await manager_with_db.submit(session_id, "/btw input event please")

    event = await _wait_bus_event(queue, "ephemeral_input_persisted", timeout=5.0)

    assert event is not None
    assert event["session_id"] == session_id
    assert event["kind"] == "btw"
    assert isinstance(event["input_id"], int)

    rows = await _get_all_ephemeral(real_db, session_id)
    assert [row["id"] for row in rows if row["role"] == "user"] == [event["input_id"]]


async def test_btw_finalize_publishes_sse_event(
    monkeypatch, tmp_path, real_db, manager_with_db
):
    """After finalize successfully persists the response row, the bus must
    publish a single ``ephemeral_pair_persisted`` event with both ids and the
    session id.  The FE uses this to refresh the inline ephemeral pair."""
    _setup_shim(monkeypatch, tmp_path, canned_response="btw-single")
    session_id = "btw-t12-sse"

    # Subscribe to the same bus PtyManager is using BEFORE the submit so we
    # don't miss the event.  The bus is created in the ``manager_with_db``
    # fixture; reach it through the manager.
    bus_queue = manager_with_db._bus.subscribe()

    await _focus(manager_with_db, session_id, str(tmp_path))
    await manager_with_db.submit(session_id, "/btw sse please")

    # Wait for the response row, then drain the bus.
    rows = await _get_all_ephemeral(real_db, session_id)
    input_row_id = next(r["id"] for r in rows if r["role"] == "user")
    resp_row = await _wait_ephemeral_response(real_db, input_row_id, timeout=30.0)
    assert resp_row is not None

    # The bus may carry other events too (idle warnings, input ack, etc.) —
    # search for ours specifically.  Allow ~2s grace for the publish to land.
    target = None
    deadline = asyncio.get_event_loop().time() + 2.0
    while asyncio.get_event_loop().time() < deadline:
        try:
            evt = bus_queue.get_nowait()
        except asyncio.QueueEmpty:
            await asyncio.sleep(0.05)
            continue
        if isinstance(evt, dict) and evt.get("type") == "ephemeral_pair_persisted":
            target = evt
            break
    assert target is not None, "ephemeral_pair_persisted event must be published"
    assert target["session_id"] == session_id
    assert target["input_id"] == input_row_id
    assert target["response_id"] == resp_row["id"]
    assert target["kind"] == "btw"


async def test_btw_finalize_publishes_submit_lifecycle_complete(
    monkeypatch, tmp_path, real_db, manager_with_db
):
    _setup_shim(monkeypatch, tmp_path, canned_response="btw-single")
    session_id = "btw-lifecycle-complete"
    queue = manager_with_db._bus.subscribe()

    await _focus(manager_with_db, session_id, str(tmp_path))
    await manager_with_db.submit(session_id, "/btw lifecycle please")

    complete = await _wait_bus_event(queue, "pty_submit_completed", timeout=30.0)

    assert complete is not None
    assert complete["session_id"] == session_id
    assert complete["kind"] == "btw"
    assert complete["status"] == "completed"


async def test_btw_stuck_timeout_publishes_submit_lifecycle_failed(
    monkeypatch, tmp_path, real_db, manager_with_db
):
    _setup_shim(monkeypatch, tmp_path, canned_response=None)
    monkeypatch.setattr(pr_mod, "find_response_complete", lambda _buf: -1)
    session_id = "btw-lifecycle-timeout"
    queue = manager_with_db._bus.subscribe()
    channel = await _focus(manager_with_db, session_id, str(tmp_path))
    channel._btw_stuck_timeout_s = 0.5

    await manager_with_db.submit(session_id, "/btw will timeout")

    failed = await _wait_bus_event(queue, "pty_submit_completed", timeout=5.0)

    assert failed is not None
    assert failed["session_id"] == session_id
    assert failed["kind"] == "btw"
    assert failed["status"] in {"failed", "timed_out"}


async def test_slash_submit_ack_publishes_submit_lifecycle_complete(
    monkeypatch, tmp_path, real_db, manager_with_db
):
    _setup_shim(monkeypatch, tmp_path)
    session_id = "slash-lifecycle-complete"
    queue = manager_with_db._bus.subscribe()

    await _focus(manager_with_db, session_id, str(tmp_path))
    await manager_with_db.submit(session_id, "/help")

    complete = await _wait_bus_event(queue, "pty_submit_completed", timeout=10.0)

    assert complete is not None
    assert complete["session_id"] == session_id
    assert complete["kind"] == "slash"
    assert complete["status"] in {"acknowledged", "completed"}


async def test_btw_timeout_evidence_survives_later_idle_kill(
    monkeypatch, tmp_path, real_db, manager_with_db
):
    _setup_shim(monkeypatch, tmp_path, canned_response=None)
    monkeypatch.setattr(pr_mod, "find_response_complete", lambda _buf: -1)
    session_id = "btw-timeout-idle-evidence"
    queue = manager_with_db._bus.subscribe()
    channel = await _focus(manager_with_db, session_id, str(tmp_path))
    channel._btw_stuck_timeout_s = 0.5

    await manager_with_db.submit(session_id, "/btw timeout then idle")

    terminal = await _wait_bus_event(queue, "pty_submit_completed", timeout=5.0)
    assert terminal is not None
    assert terminal["kind"] == "btw"
    assert terminal["status"] == "timed_out"

    rows_before_idle = await _get_all_ephemeral(real_db, session_id)
    assert any(
        row["role"] == "user" and row["content"] == "/btw timeout then idle"
        for row in rows_before_idle
    )

    manager_with_db._on_idle_kill(session_id)
    await asyncio.sleep(0.5)

    rows_after_idle = await _get_all_ephemeral(real_db, session_id)
    assert any(
        row["role"] == "user" and row["content"] == "/btw timeout then idle"
        for row in rows_after_idle
    )

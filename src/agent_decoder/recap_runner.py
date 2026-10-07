"""Recap generation — drives a hidden PTY against a forked session.

The recap path uses the same subscription-backed TUI behavior as chat submit,
so it stays inside the no-additional-cost envelope.

How it works:
  1. The caller pre-mints a UUID for the fork and tombstones it in the
     server's deleted-sessions set BEFORE invoking us — otherwise the
     watcher would briefly index the fork JSONL into the sidebar between
     file creation and our cleanup.
  2. We spawn ``claude --session-id <fork> --resume <source> --fork-session
     --model haiku --permission-mode dontAsk`` in a hidden PTY (no UI).
  3. After the TUI signals ready, we write the recap prompt + Enter.
  4. We poll the fork's JSONL for an assistant entry with ``stop_reason``
     set (the canonical "turn complete" signal) and return its text.
  5. The PTY is killed and the fork JSONL is deleted in the ``finally``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

from .pty_runner import DEFAULT_COLS, DEFAULT_ROWS, PtyChannel, _pty_env

_log = logging.getLogger(__name__)


async def generate_recap(
    session_id: str,
    *,
    cwd: str,
    bin_name: str,
    prompt: str,
    source_jsonl_path: Path,
    fork_id: str,
    timeout_seconds: float = 90.0,
) -> Optional[str]:
    """Drive a forked PTY against ``session_id`` to generate a recap.

    Returns the assistant's text on success, or ``None`` on
    spawn failure / readiness timeout / no assistant block within
    ``timeout_seconds`` / non-text content.

    The fork JSONL lives at ``source_jsonl_path.parent / f"{fork_id}.jsonl"``
    (claude writes forks alongside the source) and is deleted on exit.
    """
    fork_jsonl = source_jsonl_path.parent / f"{fork_id}.jsonl"

    # Note: we deliberately do NOT pass ``--model``. Earlier recap code forced
    # ``haiku`` for cost reduction, but PTY-TUI turns are subscription-included
    # so there's no per-call cost to minimise. Forcing a model alias also
    # turned out to be brittle
    # against cc-mirror profiles that map Claude aliases to their own
    # backends (e.g. zai's GLM mapping silently dropped requests with
    # ``--model haiku``). Letting the fork inherit the source session's
    # model — what TUI does when ``--model`` is omitted — is both
    # simpler and provider-agnostic.
    argv = [
        bin_name,
        "--session-id",
        fork_id,
        "--resume",
        session_id,
        "--fork-session",
        "--permission-mode",
        "dontAsk",
    ]
    env = await _pty_env(DEFAULT_ROWS, DEFAULT_COLS, bin_name)

    channel = PtyChannel(
        session_id=f"recap-{fork_id}",
        argv=argv,
        cwd=cwd,
        env=env,
        # Skip the ownership sidecar — the fork JSONL doesn't exist yet
        # and we're going to delete it on the way out, so there's nothing
        # to lock or hand off.
        jsonl_path=None,
    )

    try:
        await channel.start()
        ready = await channel.await_ready(timeout_s=5.0)
        if not ready:
            _log.warning(
                "recap: TUI didn't signal ready within 5s (session %s, fork %s)",
                session_id,
                fork_id,
            )
            return None
        # The fork has to load the source session's history before it can
        # accept input — for sessions with 10+ KB of history the TUI is
        # rendering its prompt box well before its input handler is
        # actually attached. Settling 3 s post-ready is empirically
        # reliable across the cwd-restore + permission-mode-banner
        # bootstrap path observed against the zai TUI build.
        await asyncio.sleep(3.0)
        # Write the prompt one byte at a time with a small inter-byte
        # delay. A single bulk ``os.write`` of the full prompt arrives
        # faster than the fork's input loop drains it and the bytes
        # appear to be discarded (no echo, no JSONL write, no error —
        # zai TUI just sits at its prompt). Char-by-char mimics human
        # typing and exercises the same input path interactive users
        # take, so the fork picks the prompt up reliably.
        body = prompt.replace("\n", "\x0a").encode("utf-8")
        try:
            for byte in body:
                channel.write(bytes([byte]))
                await asyncio.sleep(0.005)
            channel.write(b"\r")
        except Exception as exc:
            _log.warning(
                "recap: PTY write failed (session %s, fork %s): %s",
                session_id,
                fork_id,
                exc,
            )
            return None
        text = await _await_assistant_text(fork_jsonl, timeout_seconds)
        if text is None:
            _log.warning(
                "recap: no assistant text in fork JSONL within %.1fs "
                "(session %s, fork %s)",
                timeout_seconds,
                session_id,
                fork_id,
            )
        return text
    finally:
        try:
            await channel.kill()
        except Exception as exc:  # pragma: no cover — defensive
            _log.debug("recap: kill raised (fork %s): %s", fork_id, exc)
        try:
            fork_jsonl.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            _log.debug("recap: fork JSONL unlink failed (%s): %s", fork_jsonl, exc)


async def _await_assistant_text(jsonl: Path, timeout_seconds: float) -> Optional[str]:
    """Poll ``jsonl`` for an assistant entry with ``stop_reason`` set.

    JSONL is the canonical channel (Phase 0 / Phase 1 findings) — TUI
    ANSI output is fragile to parse. Returns the joined text content of
    the final assistant block, or ``None`` on timeout.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    last_size = 0
    while loop.time() < deadline:
        if jsonl.exists():
            try:
                content = jsonl.read_bytes()
            except OSError:
                content = b""
            if len(content) > last_size:
                last_size = len(content)
                text = _final_assistant_text(content)
                if text:
                    return text
        await asyncio.sleep(0.1)
    return None


def _final_assistant_text(content: bytes) -> Optional[str]:
    """Return the assistant text for the *recap turn* — the assistant entry
    written after the latest user entry.

    A forked session's JSONL starts with the entire source conversation
    copied in (that's how ``--fork-session`` works). Without anchoring on
    "latest user entry", a naive reverse scan would return the source's
    last assistant message and declare the recap done before the new turn
    even started. We find the latest user entry first, then look for an
    assistant entry after it whose ``stop_reason`` is set.
    """
    lines = content.decode("utf-8", errors="replace").splitlines()

    # Index the latest user entry (the recap prompt we just wrote).
    last_user_idx: Optional[int] = None
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i].strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") == "user":
            last_user_idx = i
            break
    if last_user_idx is None:
        return None

    # Now find the latest assistant entry AFTER that user line. Streaming
    # writes emit several partial assistant entries before the final one;
    # only the entry where ``stop_reason`` is set is the turn-complete
    # signal we want.
    for i in range(len(lines) - 1, last_user_idx, -1):
        line = lines[i].strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") != "assistant":
            continue
        msg = d.get("message")
        if not isinstance(msg, dict) or not msg.get("stop_reason"):
            continue
        parts: list[str] = []
        c = msg.get("content")
        if isinstance(c, list):
            for block in c:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                ):
                    parts.append(block["text"])
        text = "\n".join(parts).strip()
        if text:
            return text
    return None

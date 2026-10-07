#!/usr/bin/env python3
"""Fake ``claude`` TUI binary for PTY-runner tests.

Requires a real TTY on stdin (os.isatty(0)).  If stdin is not a TTY the
script exits with code 2 so the PTY-runner tests can assert that the
runner correctly attached a slave fd.

Behaviour mirrors what Phase-0 empirical testing confirmed about the real
claude TUI:
  - Inline (not alt-screen) render: emits a minimal banner so tests can
    detect the "render-ready" signal via byte-threshold polling.
  - Reads stdin in cbreak mode (ICANON off, ECHO off); echoes printable
    bytes back so the master-fd drain sees them.
  - On CR (0x0D): treats accumulated buffer as the submitted turn, writes
    two JSONL entries (user + assistant echo) to the session file, and
    clears the buffer.
  - On 0x03 (Ctrl-C): restores terminal and exits 0.

Phase 2 extension — /btw modal simulation:
  When a submitted line starts with ``/btw``:
    - The user+assistant JSONL entries are NOT written (mirrors real
      claude: /btw is ephemeral and leaves no JSONL trace).
    - Instead, the fake emits a synthetic BTW modal render to PTY stdout.
    - The exact render variant is controlled by ``--canned-response``:
        ``btw-single``  Variant A: single-line ESC[K-terminated final write.
        ``btw-multi``   Variant B: columnar multi-line write.
        (anything else) Variant A with the canned-response value as text.
    - After the response the footer (containing BTW_RESPONSE_COMPLETE_MARKER)
      is emitted.
    - On the next ESC byte (0x1b), the fake redraws the normal prompt.
  Normal (non-/btw) submits always write JSONL as before.

Flags:
  --capture-env FILE    Write JSON dump of os.environ to FILE on startup.
  --capture-argv FILE   Write JSON list of sys.argv to FILE on startup.
  --session-id SID      Set session UUID (also accepted as --resume SID
                        to mirror real claude argv shape).
  --canned-response VAL Control the canned response text.
                        Special values: ``btw-single``, ``btw-multi``.

Environment:
  CLAUDE_CONFIG_DIR     Root for JSONL output (defaults to ~/.claude).
                        Tests set this to tmp_path so files land cleanly.

JSONL path computation mirrors real claude:
  <config_dir>/projects/<encoded_cwd>/<session_id>.jsonl
  where encoded_cwd = '-' + cwd.replace('/', '-')

Shim injection (same pattern as fake_claude.py):
  Write a thin wrapper:
      #!/usr/bin/env python3
      import sys, os
      os.execv(sys.executable, [sys.executable,
          "/abs/path/to/fake_claude_tui.py"] + sys.argv[1:])
  under the desired bin_name into a tmp dir and prepend that dir to PATH.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import termios
import tty
import uuid
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Argv parsing
# ---------------------------------------------------------------------------


def _parse_argv(argv: list[str]) -> dict:
    opts: dict = {
        "capture_env": None,
        "capture_argv": None,
        "session_id": None,
        # Recap test hook: when set, the assistant entry written on submit
        # uses this exact text instead of echoing the user input. Lets the
        # recap integration tests assert against a known response.
        "canned_response": None,
    }
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--capture-env" and i + 1 < len(argv):
            opts["capture_env"] = argv[i + 1]
            i += 1
        elif a == "--capture-argv" and i + 1 < len(argv):
            opts["capture_argv"] = argv[i + 1]
            i += 1
        elif a == "--canned-response" and i + 1 < len(argv):
            opts["canned_response"] = argv[i + 1]
            i += 1
        elif a == "--session-id" and i + 1 < len(argv):
            # ``--session-id`` is the spawn's authoritative session id.
            # Real claude treats ``--session-id <new> --resume <old>
            # --fork-session`` as "fork <old> into the pre-minted <new>",
            # so the JSONL writes go to <new>. Mirror that here: prefer
            # --session-id when both are present.
            opts["session_id"] = argv[i + 1]
            i += 1
        elif a == "--resume" and i + 1 < len(argv):
            # Only set if --session-id hasn't already pinned it.
            if opts["session_id"] is None:
                opts["session_id"] = argv[i + 1]
            i += 1
        # Unknown flags (--model, --permission-mode, --fork-session, etc.)
        # are silently ignored.
        i += 1
    return opts


# ---------------------------------------------------------------------------
# JSONL path helpers
# ---------------------------------------------------------------------------


def _encode_cwd(cwd: str) -> str:
    """Encode a filesystem path the same way real claude does."""
    return "-" + cwd.replace("/", "-")


def _jsonl_path(session_id: str) -> Path:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
    encoded = _encode_cwd(os.getcwd())
    parent = Path(config_dir) / "projects" / encoded
    parent.mkdir(parents=True, exist_ok=True)
    return parent / f"{session_id}.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_jsonl(path: Path, obj: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")
        f.flush()


# ---------------------------------------------------------------------------
# BTW modal simulation (Phase 2)
# ---------------------------------------------------------------------------

# These must match the constants in btw_capture.py exactly.
_BTW_MODAL_OPEN_MARKER = b"without interrupting the main conversation"
_BTW_RESPONSE_COMPLETE_MARKER = b"\xe2\x86\x91/\xe2\x86\x93"  # ↑/↓


def _emit_btw_modal(response_text: str, variant: str) -> None:
    """Emit a synthetic /btw modal render to stdout.

    ``variant`` controls the response rendering:
      ``btw-single``  Variant A: single ESC[NC ESC[MA <text> ESC[K write.
      ``btw-multi``   Variant B: columnar multi-line write via ESC[NG.
      (anything else) Variant A with response_text as-is.
    """
    fd = sys.stdout.fileno()

    # Modal open: emit the modal header marker.
    # The full header would include "Ask a quick side question " before the
    # marker, but for tests only the stable tail fragment matters.
    os.write(fd, b"Ask a quick side question " + _BTW_MODAL_OPEN_MARKER + b"\r\n")

    # Brief "Answering..." status line.
    os.write(fd, "Answering\xe2\x80\xa6\r\n".encode())

    if variant == "btw-multi":
        # Variant B: columnar multi-line response.
        # Mirrors the real TUI pattern from the spike: the LAST ESC[NC ESC[MA
        # in the render block is NOT followed by a colour escape — it is
        # immediately followed by printable text. Then additional words are
        # placed via absolute column ESC[NG jumps.
        lines = (
            response_text.splitlines()
            if "\n" in response_text
            else [
                response_text,
                "Second line of the multi-line response.",
                "Third line here.",
            ]
        )
        for i, line in enumerate(lines):
            # ESC[4C ESC[3A cursor-rel sequence immediately followed by the
            # first word of the line (no ESC between — this is the key
            # distinguisher from animation frames for Variant B detection).
            words = line.split()
            if not words:
                continue
            # First word is written directly after the cursor-rel sequence.
            first_word = words[0]
            os.write(fd, b"\x1b[4C\x1b[3A" + first_word.encode())
            # Remaining words placed at absolute columns via ESC[NG.
            col = 5 + len(first_word) + 1
            for word in words[1:]:
                os.write(fd, f"\x1b[{col}G{word}".encode())
                col += len(word) + 1
            # Line separator: bare CR + CRLF (mirrors the spike's pattern).
            os.write(fd, b"\r\r\n")
    else:
        # Variant A: single-line ESC[K-terminated final write.
        # Pattern: ESC[NC ESC[MA <text> ESC[K
        text_bytes = response_text.encode("utf-8")
        os.write(fd, b"\x1b[4C\x1b[3A" + text_bytes + b"\x1b[K\r\n")

    # Footer with BTW_RESPONSE_COMPLETE_MARKER.
    # The full footer is "↑/↓scroll · f to fork · Esc to close" but the
    # stable capture marker is just the leading ↑/↓ bytes.
    os.write(
        fd,
        _BTW_RESPONSE_COMPLETE_MARKER
        + b"scroll \xc2\xb7 f to fork \xc2\xb7 Esc to close\r\n",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    # ``auth status`` probe — agent-decoder's pty_runner calls this before every
    # PTY spawn to decide whether to strip API-key vars. Mimic a subscription
    # response so existing strip-behavior tests keep passing; do NOT honour
    # --capture-* here (the probe is invisible to the spawn-under-test).
    _is_auth_probe = any(
        sys.argv[i] == "auth" and sys.argv[i + 1] == "status"
        for i in range(len(sys.argv) - 1)
    )
    if _is_auth_probe:
        method = os.environ.get("FAKE_CLAUDE_AUTH_METHOD", "claude.ai")
        logged_in = method != "none"
        sys.stdout.write(
            json.dumps({"authMethod": method, "loggedIn": logged_in}) + "\n"
        )
        sys.stdout.flush()
        return 0

    # --- capture flags before TTY check so --capture-env is always honoured ---
    opts = _parse_argv(sys.argv[1:])

    if opts["capture_argv"]:
        with open(opts["capture_argv"], "w", encoding="utf-8") as f:
            json.dump(sys.argv, f)

    if opts["capture_env"]:
        with open(opts["capture_env"], "w", encoding="utf-8") as f:
            json.dump(dict(os.environ), f)

    # --- TTY guard ---
    if not os.isatty(0):
        sys.stderr.write("fake_claude_tui requires a TTY\n")
        sys.stderr.flush()
        return 2

    session_id = opts["session_id"] or str(uuid.uuid4())
    jsonl_path = _jsonl_path(session_id)

    # --- terminal setup: cbreak mode (ICANON off, ECHO off) ---
    fd = sys.stdin.fileno()
    old_attrs = termios.tcgetattr(fd)

    def _restore() -> None:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)

    # Banner — tests byte-poll for this to detect render-ready.
    # \x1b7 = DECSC (save cursor), \x1b[?2004h = bracketed paste (real claude emits this)
    sys.stdout.write("\x1b7\x1b[?2004h✳ fake claude tui\r\n> ")
    sys.stdout.flush()

    line_buf: list[str] = []
    # True while we've emitted a btw modal and are waiting for ESC dismiss.
    in_btw_modal: bool = False

    def _handle_sigint(_sig: int, _frame: object) -> None:
        _restore()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _handle_sigint)

    try:
        # Real claude TUI reads stdin in raw mode (no input translation),
        # so ``\r`` reaches the reader as 0x0D. ``tty.setcbreak`` leaves
        # ICRNL on, which would translate 0x0D → 0x0A and break the
        # submit handler below — see Phase 8 P8.Impl debugging notes.
        tty.setraw(fd)

        while True:
            ch = os.read(fd, 1)
            if not ch:
                break

            byte = ch[0]

            if byte == 0x03:  # Ctrl-C
                break

            if byte == 0x1B and in_btw_modal:
                # ESC dismiss — tear down the modal and redraw normal prompt.
                in_btw_modal = False
                # Emit the erase-line sequences real claude sends on ESC.
                os.write(fd, b"\x1b[2K\x1b[1A\x1b[2K\x1b[G\x1b[1A")
                sys.stdout.write("\r\n> ")
                sys.stdout.flush()
                continue

            if byte == 0x0D:  # CR — submit
                text = "".join(line_buf)
                line_buf.clear()

                # Echo newline so output looks natural on the PTY.
                sys.stdout.write("\r\n")
                sys.stdout.flush()

                # --- /btw arm ---
                if text.lstrip().lower().startswith("/btw"):
                    # Phase 2: /btw is ephemeral — do NOT write JSONL.
                    # Emit the synthetic modal render instead.
                    canned = opts["canned_response"]
                    if canned in ("btw-single", "btw-multi"):
                        variant = canned
                        response_text = "BTW response text."
                    else:
                        variant = "btw-single"
                        response_text = (
                            canned if canned is not None else "BTW response text."
                        )
                    _emit_btw_modal(response_text, variant)
                    sys.stdout.flush()
                    in_btw_modal = True
                    continue

                # --- Normal submit arm ---
                sid = session_id
                ts = _now_iso()

                user_entry = {
                    "type": "user",
                    "uuid": str(uuid.uuid4()),
                    "sessionId": sid,
                    "timestamp": ts,
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": text}],
                    },
                }
                response_text = (
                    opts["canned_response"]
                    if opts["canned_response"] is not None
                    else f"echo: {text}"
                )
                asst_entry = {
                    "type": "assistant",
                    "uuid": str(uuid.uuid4()),
                    "sessionId": sid,
                    "timestamp": _now_iso(),
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": response_text}],
                        # ``stop_reason: end_turn`` is the canonical
                        # turn-complete signal real claude emits at the
                        # end of a streaming response. The recap runner's
                        # JSONL poll filters on it so partial chunks
                        # don't get returned as the final recap.
                        "stop_reason": "end_turn",
                    },
                }
                _append_jsonl(jsonl_path, user_entry)
                _append_jsonl(jsonl_path, asst_entry)

                # Re-emit prompt.
                sys.stdout.write("> ")
                sys.stdout.flush()

            elif 0x20 <= byte <= 0x7E:  # printable ASCII
                c = chr(byte)
                line_buf.append(c)
                sys.stdout.write(c)
                sys.stdout.flush()

            # Other control bytes (arrow keys, etc.) are silently dropped.

    finally:
        _restore()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

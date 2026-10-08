"""BTW modal capture — pure extraction functions for /btw PTY output.

The /btw command opens an inline modal inside the existing PTY screen buffer
(no alt-screen). The response is rendered via cursor-relative in-place writes.
Each position goes through a loading animation before the final text is written.

Full spike findings: docs/pty-runner-phase2-spike.md

Mechanics summary
-----------------
- Modal open: text ``without interrupting the main conversation`` appears as a
  continuous byte sequence within ~300 bytes after /btw is echoed. NOTE: the
  surrounding words ("Ask a quick side question") may be separated by column-
  positioning CSI sequences in some TUI versions, so only the tail fragment is
  used as the stable marker.
- Response region: bounded by the "Answering…" status marker (start) and the
  ``↑/↓`` arrow bytes (``\\xe2\\x86\\x91/\\xe2\\x86\\x93``) that open the footer
  (end). The full footer text ``↑/↓scroll · f to fork · Esc to close`` may
  have column-positioning escapes interspersed, so only the leading ``↑/↓``
  bytes are used as the marker.
- Two rendering variants observed in the field:

  *Variant A (single-line, old TUI)*: final write uses cursor-relative
  positioning with ESC[K suffix::

      ESC [ <N> C  ESC [ <M> A  <text>  ESC [ K

  *Variant B (multi-line, newer TUI)*: the last ``ESC[NC ESC[MA`` sequence
  that is NOT followed by a colour escape starts the response block. Words are
  placed with absolute column jumps ``ESC[NG``; lines separated by ``\\r``
  or ``\\r\\r\\n`` followed by re-positioning sequences.

- Both variants: animation frames use colour wrappers
  (``ESC[38;2;…m <char> ESC[39m``) and are distinguished from final writes by
  the ABSENCE of a trailing ESC[K (variant A) or by being followed immediately
  by another colour-wrapped frame or by being a single character (variant B).
- Dismiss: send ESC after the footer appears; allow ~2 s for TUI redraw.

Multi-line addendum (2026-05-28): see docs/pty-runner-phase2-spike.md.
"""

from __future__ import annotations

import re
from typing import Optional


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

# Stable tail fragment of the modal header text. Both TUI variants render
# "without interrupting the main conversation" as a continuous byte sequence.
# The leading "Ask a quick side question" may have column-jump escapes between
# words in some versions, so we anchor on this suffix only.
BTW_MODAL_OPEN_MARKER: bytes = b"without interrupting the main conversation"

# The ``↑/↓`` arrow bytes that open the footer line.  The full text
# "↑/↓scroll · f to fork · Esc to close" may have CSI column-jumps between
# words; only the leading arrow pair is stable across TUI versions.
BTW_RESPONSE_COMPLETE_MARKER: bytes = b"\xe2\x86\x91/\xe2\x86\x93"  # "↑/↓"

BTW_DISMISS_SEQUENCE: bytes = b"\x1b"  # ESC

# ---------------------------------------------------------------------------
# Pre-compiled patterns
# ---------------------------------------------------------------------------

# Variant A: cursor-relative final write with ESC[K suffix.
# Animation frames are excluded because they lack the trailing ESC[K.
_FINAL_WRITE_RE = re.compile(rb"\x1b\[\d+C\x1b\[\d+A([^\x1b]+)\x1b\[K")

# Any cursor-relative positioning sequence ESC[NC ESC[MA (move right N, up M).
# Used to find the start of the response block in Variant B.
_CURSOR_REL_RE = re.compile(rb"\x1b\[\d+C\x1b\[\d+A")

# Case-insensitive word-boundary match for /btw at start of string.
# Mirrors claude's own regex: /^\\/btw\\b/gi
_BTW_INPUT_RE = re.compile(r"^/btw\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Input detection
# ---------------------------------------------------------------------------


def is_btw_input(content: str) -> bool:
    """Return True if *content* is a /btw command.

    Matches ``/btw`` at the start of the string followed by a word boundary
    (space, newline, or end-of-string). Case-insensitive, mirroring claude's
    own regex ``/^\\/btw\\b/gi``.

    Edge cases::

        /btw          → True
        /btw foo      → True
        /btw\\n        → True
        /BTW foo      → True   (claude's regex is /gi)
        /btw_foo      → False  (word boundary fails before _)
        /btw-foo      → True   (\\b matches before -)
        "  /btw foo"  → False  (leading whitespace not stripped)
        "x/btw foo"   → False
    """
    return bool(_BTW_INPUT_RE.match(content))


def extract_btw_input(content: str) -> str:
    """Strip the leading ``/btw`` prefix (and following whitespace) from *content*.

    Returns the bare side-question text. If *content* does not start with
    /btw, returns *content* unchanged.
    """
    stripped = _BTW_INPUT_RE.sub("", content, count=1)
    return stripped.lstrip()


# ---------------------------------------------------------------------------
# Marker search helpers
# ---------------------------------------------------------------------------


def find_modal_open(raw: bytes, start: int = 0) -> int:
    """Return the byte offset of :data:`BTW_MODAL_OPEN_MARKER` in *raw[start:]*.

    Returns -1 if not found.
    """
    return raw.find(BTW_MODAL_OPEN_MARKER, start)


def find_response_complete(raw: bytes, start: int = 0) -> int:
    """Return the byte offset of :data:`BTW_RESPONSE_COMPLETE_MARKER` in *raw[start:]*.

    Returns the offset of the START of the marker, or -1 if not found.
    """
    return raw.find(BTW_RESPONSE_COMPLETE_MARKER, start)


# ---------------------------------------------------------------------------
# Internal: column-aware text reconstruction (Variant B)
# ---------------------------------------------------------------------------


def _reconstruct_columnar(raw_fragment: bytes) -> str:
    """Reconstruct human-readable text from a byte sequence that uses
    absolute column positioning (``ESC[NG``) to place words and ``\\r`` /
    ``\\r\\r\\n`` as line separators.

    This handles the multi-line TUI rendering variant where words are
    placed at absolute terminal columns rather than written sequentially.
    Gaps between words are filled with a single space.

    Colour/attribute CSI sequences (``ESC[…m``) and unrecognised sequences
    are silently dropped.

    Returns the reconstructed text with leading/trailing whitespace stripped
    on each line, and empty lines removed.
    """
    lines: list[str] = []
    current_line: list[str] = []
    current_col: int = 0

    _TOKEN_RE = re.compile(
        rb"""
        \r\r\n                          # CR LF LF
        | \r\n                          # CR LF
        | \r                            # bare CR
        | \x1b\[(\d+)G                  # absolute column ESC[NG
        | \x1b\[\d*[ABCD]               # cursor move (ignore)
        | \x1b\[\d+;\d+[Hf]             # cursor position (ignore)
        | \x1b\[[\d;]*[a-zA-Z]          # other CSI (colour, attrs, etc.)
        | \x1b[^[]                       # other ESC
        | ([^\x1b\r\n]+)                 # printable text
    """,
        re.VERBOSE,
    )

    for m in _TOKEN_RE.finditer(raw_fragment):
        raw_tok = m.group(0)
        abs_col = m.group(1)
        text = m.group(2)

        if raw_tok in (b"\r\r\n", b"\r\n", b"\r"):
            lines.append("".join(current_line))
            current_line = []
            current_col = 0
        elif abs_col is not None:
            target = int(abs_col)
            if target > current_col:
                current_line.append(" ")  # one space for any column gap
            current_col = target
        elif text is not None:
            decoded = text.decode("utf-8", errors="replace")
            current_line.append(decoded)
            current_col += len(decoded)

    if current_line:
        lines.append("".join(current_line))

    cleaned = [ln.strip() for ln in lines if ln.strip()]
    return "\n".join(cleaned)


# ---------------------------------------------------------------------------
# Response extractor
# ---------------------------------------------------------------------------


def extract_btw_response(
    raw: bytes,
    open_offset: Optional[int] = None,
) -> Optional[str]:
    """Extract assistant text from a /btw modal capture.

    If *open_offset* is ``None``, scans for :data:`BTW_MODAL_OPEN_MARKER`
    from the start of *raw*. Bounds the search at
    :data:`BTW_RESPONSE_COMPLETE_MARKER` so the footer text and post-modal
    redraws do not contaminate the result.

    Two rendering variants are handled:

    *Variant A (single-line)*: Collect ALL matches of
    ``\\x1b[\\d+C\\x1b[\\d+A([^\\x1b]+)\\x1b[K`` in the region (final writes;
    animation frames lack the trailing ``\\x1b[K``). Decode each match,
    strip, join with ``"\\n"``.

    *Variant B (multi-line)*: Find the last ``\\x1b[\\d+C\\x1b[\\d+A``
    sequence in the region that is NOT immediately followed by ``\\x1b[``
    (i.e., not an animation frame's colour wrapper). Everything from that
    cursor sequence onward is the response block; reconstruct human-readable
    text using column-aware parsing.

    If both variants yield results, Variant A takes precedence (it is the
    more precise pattern when applicable).

    Returns the extracted text or ``None`` if extraction failed.
    """
    # Resolve open offset.
    #
    # The modal-open text marker isn't always rendered as a contiguous byte
    # sequence — newer zai TUI versions intersperse cursor-positioning
    # escapes between the header words, breaking ``bytes.find``.  When the
    # marker is missing, fall back to scanning the entire buffer: the live
    # PTY accumulator only feeds bytes while ``expecting_btw_response=True``,
    # which is set immediately after the /btw input is written.  The buffer
    # therefore starts at-or-after the /btw bytes, so scanning from offset 0
    # won't pick up unrelated assistant content from before the modal.
    # Variant A's pattern (``\x1b[\d+C\x1b[\d+A...\x1b[K``) is specific to
    # the modal's per-character final-write rendering and won't false-match
    # on regular TUI output.
    if open_offset is None:
        found = find_modal_open(raw)
        open_offset = found if found >= 0 else 0
    if open_offset < 0 or open_offset >= len(raw):
        return None

    # Bound the search region at the response-complete marker
    end_offset = find_response_complete(raw, open_offset)
    if end_offset >= 0:
        region = raw[open_offset:end_offset]
    else:
        # Graceful degradation: no footer seen yet (truncated capture)
        region = raw[open_offset:]

    # --- Variant A: single-line ESC[K-terminated final writes ----------------
    lines_a: list[str] = []
    for m in _FINAL_WRITE_RE.finditer(region):
        text = m.group(1).decode("utf-8", errors="replace").strip()
        if text:
            lines_a.append(text)

    if lines_a:
        return "\n".join(lines_a)

    # --- Variant B: multi-line columnar rendering ----------------------------
    # Find the last cursor-relative sequence NOT followed by a colour escape.
    # Animation: ESC[NC ESC[MA ESC[38;...m   (colour immediately after)
    # Final write: ESC[NC ESC[MA <printable>  (no immediate ESC)
    all_cursor = list(_CURSOR_REL_RE.finditer(region))
    for m in reversed(all_cursor):
        after = region[m.end() : m.end() + 2]
        if not after.startswith(b"\x1b["):
            # This is the start of the response block
            response_raw = region[m.end() :]
            result = _reconstruct_columnar(response_raw)
            if result:
                return result
            break

    return None

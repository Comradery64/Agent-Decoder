"""Tests for btw_capture.py — pure extraction functions, no PTY interaction."""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "btw_capture"

# ---------------------------------------------------------------------------
# Helpers: synthetic byte fixtures
# ---------------------------------------------------------------------------

# Stable tail fragment of the modal header.
_MODAL_OPEN = b"without interrupting the main conversation"

# ↑/↓ arrow bytes that open the footer line.
_FOOTER = b"\xe2\x86\x91/\xe2\x86\x93"  # "↑/↓"


def _animation_frame(char: str) -> bytes:
    """Synthesise one animation frame (colour-wrapped, no ESC[K)."""
    return b"\x1b[4C\x1b[3A\x1b[38;2;255;193;7m" + char.encode() + b"\x1b[39m"


def _final_write_v1(text: str, cols: int = 4, rows: int = 3) -> bytes:
    """Synthesise a Variant A final write (cursor-relative + ESC[K suffix)."""
    return f"\x1b[{cols}C\x1b[{rows}A".encode() + text.encode("utf-8") + b"\x1b[K"


def _final_write_v2(text: str, cols: int = 4, rows: int = 3) -> bytes:
    """Synthesise a Variant B final write (cursor-relative, no ESC[K).

    For single-word text this is just the prefix + text.
    Multi-word text uses column-absolute gaps (simplified here as one \x1b[NG
    between words for testing purposes).
    """
    words = text.split(" ")
    buf = f"\x1b[{cols}C\x1b[{rows}A".encode() + words[0].encode("utf-8")
    col = len(words[0])
    for word in words[1:]:
        col += 1
        buf += f"\x1b[{col + len(word)}G".encode() + word.encode("utf-8")
        col += len(word)
    return buf


# ============================================================================
# is_btw_input
# ============================================================================


class TestIsBtwInput:
    def test_bare_slash_btw(self):
        from agent_decoder.btw_capture import is_btw_input

        assert is_btw_input("/btw") is True

    def test_slash_btw_with_text(self):
        from agent_decoder.btw_capture import is_btw_input

        assert is_btw_input("/btw foo") is True

    def test_slash_btw_with_newline(self):
        from agent_decoder.btw_capture import is_btw_input

        assert is_btw_input("/btw\n") is True

    def test_uppercase_btw(self):
        from agent_decoder.btw_capture import is_btw_input

        # claude's regex is /gi (case-insensitive)
        assert is_btw_input("/BTW foo") is True

    def test_mixed_case_btw(self):
        from agent_decoder.btw_capture import is_btw_input

        assert is_btw_input("/Btw something") is True

    def test_btw_underscore_suffix_false(self):
        from agent_decoder.btw_capture import is_btw_input

        # \b does not match between 'w' and '_' (both \w)
        assert is_btw_input("/btw_foo") is False

    def test_btw_hyphen_suffix_true(self):
        from agent_decoder.btw_capture import is_btw_input

        # \b matches between 'w' (word) and '-' (non-word)
        assert is_btw_input("/btw-foo") is True

    def test_leading_whitespace_false(self):
        from agent_decoder.btw_capture import is_btw_input

        assert is_btw_input("  /btw foo") is False

    def test_prefix_char_false(self):
        from agent_decoder.btw_capture import is_btw_input

        assert is_btw_input("x/btw foo") is False

    def test_empty_string_false(self):
        from agent_decoder.btw_capture import is_btw_input

        assert is_btw_input("") is False


# ============================================================================
# extract_btw_input
# ============================================================================


class TestExtractBtwInput:
    def test_strips_slash_btw_space(self):
        from agent_decoder.btw_capture import extract_btw_input

        assert extract_btw_input("/btw what is 2+2?") == "what is 2+2?"

    def test_strips_bare_slash_btw(self):
        from agent_decoder.btw_capture import extract_btw_input

        assert extract_btw_input("/btw") == ""

    def test_strips_uppercase(self):
        from agent_decoder.btw_capture import extract_btw_input

        assert extract_btw_input("/BTW explain X") == "explain X"

    def test_no_btw_prefix_passthrough(self):
        from agent_decoder.btw_capture import extract_btw_input

        # Not a /btw command — returned unchanged
        assert extract_btw_input("hello world") == "hello world"

    def test_strips_leading_whitespace_after_btw(self):
        from agent_decoder.btw_capture import extract_btw_input

        assert extract_btw_input("/btw   lots of spaces") == "lots of spaces"


# ============================================================================
# find_modal_open
# ============================================================================


class TestFindModalOpen:
    def test_finds_at_start(self):
        from agent_decoder.btw_capture import find_modal_open, BTW_MODAL_OPEN_MARKER

        raw = BTW_MODAL_OPEN_MARKER + b" extra"
        assert find_modal_open(raw) == 0

    def test_finds_at_offset(self):
        from agent_decoder.btw_capture import find_modal_open, BTW_MODAL_OPEN_MARKER

        raw = b"preamble" + BTW_MODAL_OPEN_MARKER
        expected = len(b"preamble")
        assert find_modal_open(raw) == expected

    def test_not_found_returns_minus_one(self):
        from agent_decoder.btw_capture import find_modal_open

        assert find_modal_open(b"no marker here") == -1

    def test_start_param_skips_earlier_match(self):
        from agent_decoder.btw_capture import find_modal_open, BTW_MODAL_OPEN_MARKER

        raw = BTW_MODAL_OPEN_MARKER + b"---" + BTW_MODAL_OPEN_MARKER
        first = find_modal_open(raw, 0)
        second = find_modal_open(raw, first + 1)
        assert first == 0
        assert second == len(BTW_MODAL_OPEN_MARKER) + 3


# ============================================================================
# find_response_complete
# ============================================================================


class TestFindResponseComplete:
    def test_finds_footer(self):
        from agent_decoder.btw_capture import (
            find_response_complete,
            BTW_RESPONSE_COMPLETE_MARKER,
        )

        raw = b"some content " + BTW_RESPONSE_COMPLETE_MARKER + b" more"
        assert find_response_complete(raw) == len(b"some content ")

    def test_not_found_returns_minus_one(self):
        from agent_decoder.btw_capture import find_response_complete

        assert find_response_complete(b"no footer") == -1

    def test_start_param(self):
        from agent_decoder.btw_capture import (
            find_response_complete,
            BTW_RESPONSE_COMPLETE_MARKER,
        )

        raw = BTW_RESPONSE_COMPLETE_MARKER + b"---" + BTW_RESPONSE_COMPLETE_MARKER
        first = find_response_complete(raw, 0)
        second = find_response_complete(raw, first + 1)
        assert first == 0
        assert second == len(BTW_RESPONSE_COMPLETE_MARKER) + 3


# ============================================================================
# extract_btw_response
# ============================================================================


class TestExtractBtwResponse:
    # --- Fixture 1: single-line Variant A (ESC[K suffix) --------------------

    def test_single_line_variant_a(self):
        """Variant A: cursor-relative + ESC[K — extracts '4.'"""
        from agent_decoder.btw_capture import extract_btw_response

        raw = _MODAL_OPEN + _final_write_v1("4.") + _FOOTER
        result = extract_btw_response(raw)
        assert result == "4."

    def test_single_line_variant_a_longer_text(self):
        """Variant A with longer text still extracts correctly."""
        from agent_decoder.btw_capture import extract_btw_response

        raw = _MODAL_OPEN + _final_write_v1("The answer is 4") + _FOOTER
        result = extract_btw_response(raw)
        assert result == "The answer is 4"

    # --- Fixture 2: multi-line Variant A (multiple ESC[K writes) ------------

    def test_multi_line_variant_a_three_lines(self):
        """Three separate Variant A final-write sequences joined with newlines."""
        from agent_decoder.btw_capture import extract_btw_response

        raw = (
            _MODAL_OPEN
            + _final_write_v1("Line one.", cols=4, rows=3)
            + _final_write_v1("Line two.", cols=4, rows=2)
            + _final_write_v1("Line three.", cols=4, rows=1)
            + _FOOTER
        )
        result = extract_btw_response(raw)
        assert result is not None
        lines = result.split("\n")
        assert len(lines) == 3
        assert lines[0] == "Line one."
        assert lines[1] == "Line two."
        assert lines[2] == "Line three."

    # --- Fixture 3: animation frames then Variant A final write -------------

    def test_animation_frames_excluded_variant_a(self):
        """Animation frames (colour-wrapped, no ESC[K) must NOT appear in result."""
        from agent_decoder.btw_capture import extract_btw_response

        raw = (
            _MODAL_OPEN
            + _animation_frame("o")
            + _animation_frame("O")
            + _animation_frame("0")
            + _final_write_v1("The answer is 4")
            + _FOOTER
        )
        result = extract_btw_response(raw)
        assert result == "The answer is 4"

    # --- Variant B: multi-word columnar rendering ---------------------------

    def test_multi_line_variant_b(self):
        """Variant B: columnar rendering reconstructs text correctly."""
        from agent_decoder.btw_capture import extract_btw_response

        # Simulate: animation frames then final write with columnar text
        raw = (
            _MODAL_OPEN
            + _animation_frame("o")
            + _animation_frame("O")
            + _final_write_v2("Hello world")
            + _FOOTER
        )
        result = extract_btw_response(raw)
        assert result is not None
        assert "Hello" in result
        assert "world" in result

    # --- Edge: no modal-open marker → None ----------------------------------

    def test_no_modal_open_falls_back_to_buffer_start(self):
        """Newer zai TUI versions intersperse cursor escapes between the
        modal-header words, so ``find_modal_open`` may return -1 even
        though a /btw response is in the buffer.  The extractor falls
        back to scanning from offset 0 — the live accumulator only
        feeds bytes while ``expecting_btw_response=True`` so there's no
        risk of picking up pre-modal content."""
        from agent_decoder.btw_capture import extract_btw_response

        raw = _final_write_v1("some text") + _FOOTER
        result = extract_btw_response(raw)
        assert result == "some text"

    def test_no_modal_open_and_no_pattern_returns_none(self):
        """Buffer with neither modal-open marker nor any final-write
        pattern still returns None — fallback is permissive about the
        start position but still requires extractable content."""
        from agent_decoder.btw_capture import extract_btw_response

        raw = b"random noise without any modal artifacts"
        result = extract_btw_response(raw)
        assert result is None

    def test_explicit_bad_open_offset_returns_none(self):
        from agent_decoder.btw_capture import extract_btw_response

        raw = b"short"
        result = extract_btw_response(raw, open_offset=999)
        assert result is None

    # --- Edge: open marker present but no footer → graceful degradation -----

    def test_no_footer_still_extracts(self):
        """When the footer is absent (truncated capture), extract what's available."""
        from agent_decoder.btw_capture import extract_btw_response

        raw = _MODAL_OPEN + _final_write_v1("partial answer")
        # No BTW_RESPONSE_COMPLETE_MARKER — should still extract
        result = extract_btw_response(raw)
        assert result == "partial answer"

    # --- Animation + multi-line Variant A interleaved -----------------------

    def test_animation_then_multi_line_variant_a(self):
        """Each line may have its own animation cycle before its final write."""
        from agent_decoder.btw_capture import extract_btw_response

        raw = (
            _MODAL_OPEN
            + _animation_frame("o")
            + _animation_frame("O")
            + _final_write_v1("First line", cols=4, rows=3)
            + _animation_frame("o")
            + _animation_frame("O")
            + _final_write_v1("Second line", cols=4, rows=2)
            + _FOOTER
        )
        result = extract_btw_response(raw)
        assert result is not None
        assert "First line" in result
        assert "Second line" in result

    # --- Real multi-line fixture (loaded from .bin) --------------------------

    def test_real_live_zai_no_open_marker(self):
        """Live-captured zai PTY drain (2026-05-28) where the modal-open
        text marker is NOT a contiguous byte sequence in the buffer —
        newer TUI versions intersperse cursor escapes between the
        header words.  The extractor must fall back to scanning from
        offset 0 and still find Variant A's final-write pattern.
        Question was 'what is 2 plus 2?' → expected answer contains '4'.
        """
        fixture_path = FIXTURES / "live_zai_no_open_marker.bin"
        if not fixture_path.exists():
            pytest.skip("live_zai_no_open_marker.bin fixture missing")

        from agent_decoder.btw_capture import extract_btw_response, find_modal_open

        raw = fixture_path.read_bytes()
        # Sanity: the open marker really is absent in this capture.
        assert find_modal_open(raw) == -1, (
            "fixture should not contain a contiguous modal-open marker"
        )
        result = extract_btw_response(raw)
        assert result is not None, (
            "extractor must fall back to buffer start when open marker absent"
        )
        assert "4" in result, f"expected answer to contain '4', got {result!r}"

    def test_real_multiline_fixture(self):
        """Load the real captured bytes and verify multi-line extraction."""
        fixture_path = FIXTURES / "multiline.bin"
        if not fixture_path.exists():
            pytest.skip(
                "multiline.bin fixture not yet generated — run /tmp/btw-mline-spike/run.py"
            )

        from agent_decoder.btw_capture import extract_btw_response

        raw = fixture_path.read_bytes()
        result = extract_btw_response(raw)
        assert result is not None, "extract_btw_response returned None on real capture"
        assert len(result.strip()) > 0, "extracted text is empty"
        # Multi-line: Pythagorean theorem in 3 lines → expect ≥ 2 lines
        lines = [ln for ln in result.split("\n") if ln.strip()]
        assert len(lines) >= 2, (
            f"Expected multi-line response, got {len(lines)} lines: {result!r}"
        )
        # Content check: must mention Pythagorean content
        combined = result.lower()
        assert any(
            kw in combined
            for kw in ["pythagorean", "triangle", "hypotenuse", "a²", "c²"]
        ), f"Response doesn't look like Pythagorean theorem: {result!r}"

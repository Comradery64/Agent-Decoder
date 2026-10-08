from __future__ import annotations

import base64
from typing import Any


def decode_terminal_input(data: str) -> bytes:
    return data.encode("utf-8", errors="surrogatepass")


def encode_pty_output_chunk(session_id: str, chunk: bytes) -> dict[str, Any]:
    return {
        "type": "pty_output_chunk",
        "session_id": session_id,
        "data_b64": base64.b64encode(chunk).decode("ascii"),
    }


def encode_pty_snapshot(
    *,
    session_id: str,
    ring: bytes,
    ring_complete: bool,
    rows: int,
    cols: int,
    alive: bool,
    native_state: str,
    decoded_input_safe: bool,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "ring_b64": base64.b64encode(ring).decode("ascii"),
        "ring_complete": ring_complete,
        "rows": rows,
        "cols": cols,
        "alive": alive,
        "native_state": native_state,
        "decoded_input_safe": decoded_input_safe,
    }

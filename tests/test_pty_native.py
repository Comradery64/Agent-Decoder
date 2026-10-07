from agent_decoder.pty_native import (
    decode_terminal_input,
    encode_pty_output_chunk,
)


def test_decode_terminal_input_preserves_escape_sequences():
    assert decode_terminal_input("\x1b[A") == b"\x1b[A"


def test_decode_terminal_input_preserves_control_chars():
    assert decode_terminal_input("\r\x03") == b"\r\x03"


def test_encode_pty_output_chunk_is_json_safe_base64():
    payload = encode_pty_output_chunk("sess-1", b"\x1b[?2004hhello")
    assert payload["type"] == "pty_output_chunk"
    assert payload["session_id"] == "sess-1"
    assert payload["data_b64"] == "G1s/MjAwNGhoZWxsbw=="

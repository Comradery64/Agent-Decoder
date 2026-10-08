from pathlib import Path

from agent_decoder.pty_screen_state import classify_screen

FIXTURES = Path(__file__).parent / "fixtures"


def test_classifies_login_required():
    result = classify_screen("Not logged in · Please run /login")
    assert result.state == "login_required"
    assert result.decoded_input_safe is False


def test_classifies_trust_prompt():
    result = classify_screen("Do you trust the files in this folder?\n1. Yes  2. No")
    assert result.state == "trust_prompt"
    assert result.decoded_input_safe is False


def test_classifies_ask_user_question():
    result = classify_screen("AskUserQuestion\nWhich option should I use?")
    assert result.state == "ask_user_question"
    assert result.decoded_input_safe is False


def test_classifies_permission_prompt():
    result = classify_screen("Do you want to proceed?\n❯ 1. Yes\n  2. No")
    assert result.state == "permission_prompt"
    assert result.decoded_input_safe is False


def test_idle_chat_input_is_safe():
    result = classify_screen("> \nWelcome back. What would you like to do?")
    assert result.state == "idle_chat_input"
    assert result.decoded_input_safe is True


def test_normalizes_ansi_before_matching():
    # Real PTY output is escape-laden; the matcher must see through it.
    ansi = "\x1b[2J\x1b[1;1H\x1b[31mNot logged in\x1b[0m · Please run /login"
    assert classify_screen(ansi).state == "login_required"


def test_does_not_false_positive_on_ordinary_output():
    # The old heuristic flipped to "permission_prompt" on any text containing
    # "allow" + "yes"/"no" — ordinary assistant prose. It must stay safe now.
    prose = (
        "Sure — I'll allow for that. Yes, the function returns early when the "
        "list is empty; no extra handling is needed. Trust the tests here."
    )
    result = classify_screen(prose)
    assert result.state == "idle_chat_input"
    assert result.decoded_input_safe is True


def test_real_btw_capture_is_not_misclassified_as_native_prompt():
    # A real /btw answer (escape-laden ring bytes) must not be mistaken for a
    # blocking native prompt — otherwise the UI auto-switches out of Decoded.
    raw = (FIXTURES / "btw_capture" / "multiline.bin").read_bytes()
    result = classify_screen(raw.decode("utf-8", errors="replace"))
    assert result.state == "idle_chat_input"
    assert result.decoded_input_safe is True

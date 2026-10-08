from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PtyScreenClassification:
    state: str
    decoded_input_safe: bool


# Strip ANSI / VT control sequences so substring matching sees readable text
# rather than escape-laden bytes.
_CSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_OTHER_ESC = re.compile(r"\x1b[@-Z\\-_]")


def _normalize(text: str) -> str:
    text = _OSC.sub("", text)
    text = _CSI.sub("", text)
    text = _OTHER_ESC.sub("", text)
    # Replace remaining control chars (except newline) with spaces, then
    # collapse runs of spaces/tabs. Keep newlines so line-oriented phrases
    # survive.
    text = "".join(c if (c == "\n" or c.isprintable()) else " " for c in text)
    return re.sub(r"[ \t]+", " ", text).lower()


def classify_screen(text: str) -> PtyScreenClassification:
    """Best-effort classification of the live PTY screen for Native-View gating.

    IMPORTANT: the input is the flattened output *ring* (a byte log with cursor
    moves), not an emulator-rendered screen, so it is noisy and can contain
    overwritten content. We therefore match only a small set of distinctive,
    high-signal phrases and **err toward ``idle_chat_input`` (decoded input
    safe) when unsure** — a false "native required" needlessly yanks the user
    out of Decoded View and is the failure mode we most want to avoid. The
    previous heuristics (bare ``allow`` + ``yes``/``no``, bare ``trust``,
    ``?`` + ``select``) matched ordinary assistant output and caused exactly
    those spurious switches. A fully robust state machine (slash palette, model
    selector, etc.) would need real backend terminal emulation; that is out of
    scope here.
    """
    s = _normalize(text)

    # Auth/login — distinctive banners claude prints when not authenticated.
    if "not logged in" in s or "please run /login" in s or "invalid api key" in s:
        return PtyScreenClassification("login_required", False)

    # Trust prompt — Claude Code: "Do you trust the files in this folder?"
    if "do you trust the files" in s:
        return PtyScreenClassification("trust_prompt", False)

    # AskUserQuestion surfaced as a native prompt (tool name appears verbatim).
    if "askuserquestion" in s:
        return PtyScreenClassification("ask_user_question", False)

    # Tool/permission prompt — Claude Code phrasings: "Do you want to proceed?"
    # / "Do you want to allow ...". Specific enough to avoid prose false hits.
    if "do you want to proceed" in s or "do you want to allow" in s:
        return PtyScreenClassification("permission_prompt", False)

    return PtyScreenClassification("idle_chat_input", True)

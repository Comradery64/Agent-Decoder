"""AppConfig.get_active_data_sources — Codex must stay visible under any profile.

Profiles partition Claude history (personal vs work); Codex is not part of that
partition. A regression here makes every Codex session vanish from the list,
search, and analytics whenever a Claude profile is active.
"""

from __future__ import annotations

import os

from agent_decoder.models import AppConfig, Profile


def _expand(p: str) -> str:
    return os.path.expanduser(p)


def test_active_sources_include_codex_under_active_profile():
    cfg = AppConfig(
        profiles=[Profile(id="p1", name="work", data_paths=["~/.claude"])],
        active_profile_id="p1",
        codex_data_paths=["~/.codex/sessions"],
    )
    sources = cfg.get_active_data_sources()
    assert sources is not None
    assert _expand("~/.claude") in sources
    assert _expand("~/.codex/sessions") in sources


def test_custom_codex_path_is_appended():
    cfg = AppConfig(
        profiles=[Profile(id="p1", name="work", data_paths=["~/.claude"])],
        active_profile_id="p1",
        codex_data_paths=["/tmp/codex-a", "/tmp/codex-b"],
    )
    sources = cfg.get_active_data_sources()
    assert sources is not None
    assert "/tmp/codex-a" in sources and "/tmp/codex-b" in sources


def test_no_profile_still_returns_none():
    # Unchanged behavior: with no active profile, scanning is unfiltered (None).
    assert AppConfig().get_active_data_sources() is None
    assert (
        AppConfig(
            profiles=[Profile(id="p1", name="x", data_paths=["~/.claude"])]
        ).get_active_data_sources()
        is None
    )

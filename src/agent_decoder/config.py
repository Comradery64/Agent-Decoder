"""User configuration — persisted to XDG_CONFIG_HOME/clau-decode/config.json.

Contract (for Agent 2 to implement):
  load_config() -> AppConfig
    - Read from config file if it exists, else return defaults
    - Merge CLI-provided overrides (extra_paths, port)

  save_config(config: AppConfig) -> None
    - Atomically write config to the config file (write to .tmp, rename)

  get_config_path() -> Path
    - Return XDG_CONFIG_HOME/clau-decode/config.json (or ~/.config/clau-decode/config.json)

  get_db_path() -> Path
    - Return XDG_DATA_HOME/clau-decode/index.db (or ~/.local/share/clau-decode/index.db),
      transparently migrating a legacy ~/.cache DB on first use

SOLID notes:
  - No global state — callers hold the config object; this module is pure I/O
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from .models import AppConfig


def get_config_path() -> Path:
    """Return the path to the clau-decode configuration file.

    Respects the XDG Base Directory specification: uses ``XDG_CONFIG_HOME`` if
    set, otherwise falls back to ``~/.config``.

    Returns:
        ``<xdg_config>/clau-decode/config.json``
    """
    xdg_config = os.environ.get("XDG_CONFIG_HOME", "") or str(
        Path("~/.config").expanduser()
    )
    return Path(xdg_config) / "clau-decode" / "config.json"


def _legacy_cache_db_path() -> Path:
    """The old DB location under the disposable cache dir (XDG_CACHE_HOME /
    ~/.cache). Kept only to migrate data out of it — see ``get_db_path``."""
    xdg_cache = os.environ.get("XDG_CACHE_HOME", "") or str(
        Path("~/.cache").expanduser()
    )
    return Path(xdg_cache) / "clau-decode" / "index.db"


def get_db_path() -> Path:
    """Return the path to the clau-decode SQLite database.

    Stored under the DURABLE data dir (``XDG_DATA_HOME`` / ``~/.local/share``),
    NOT the cache dir: the DB holds non-regenerable user intent (archived /
    starred / viewed flags + custom titles in ``session_meta``), which an OS or
    cache cleaner could otherwise wipe. The message index is regenerable but
    lives here too so it persists across cache clears (no rescan needed).

    On first use we transparently migrate a legacy ``~/.cache`` DB to the new
    location (one-time copy, including any WAL/SHM sidecars so un-checkpointed
    writes survive). The legacy file is left in place as a backstop.

    Returns:
        ``<xdg_data>/clau-decode/index.db``
    """
    xdg_data = os.environ.get("XDG_DATA_HOME", "") or str(
        Path("~/.local/share").expanduser()
    )
    db_path = Path(xdg_data) / "clau-decode" / "index.db"

    if not db_path.exists():
        legacy = _legacy_cache_db_path()
        if legacy.exists():
            db_path.parent.mkdir(parents=True, exist_ok=True)
            # Copy the main DB plus WAL/SHM so recent (un-checkpointed) writes
            # — e.g. the latest archive/star — aren't lost in the move.
            for suffix in ("", "-wal", "-shm"):
                src = Path(str(legacy) + suffix)
                if src.exists():
                    shutil.copy2(src, Path(str(db_path) + suffix))
    return db_path


def load_config(
    extra_paths: list[str] | None = None, port: int | None = None
) -> AppConfig:
    """Load config from disk and apply any CLI overrides.

    If the config file does not exist, a default ``AppConfig()`` is returned.
    CLI overrides are applied after loading:
      - ``extra_paths``: appended to ``config.data_paths`` (deduplicated, order
        preserved).
      - ``port``: replaces ``config.port`` when provided.

    Args:
        extra_paths: Additional scan paths supplied via ``--path`` flags.
        port:        Port override supplied via ``--port`` flag.

    Returns:
        The resolved ``AppConfig``.
    """
    path = get_config_path()

    if path.exists():
        raw = path.read_text(encoding="utf-8")
        config = AppConfig.model_validate(json.loads(raw))
    else:
        config = AppConfig()

    if extra_paths:
        existing = set(config.data_paths)
        for p in extra_paths:
            if p not in existing:
                config.data_paths.append(p)
                existing.add(p)

    if port is not None:
        config.port = port

    return config


def save_config(config: AppConfig) -> None:
    """Atomically persist config to disk.

    Writes to a ``.tmp`` file first, then uses ``os.replace`` for an atomic
    rename so readers never see a partial write.  Parent directories are
    created automatically.

    Args:
        config: The ``AppConfig`` to persist.
    """
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(config.model_dump_json(indent=2), encoding="utf-8")
    os.replace(tmp_path, path)

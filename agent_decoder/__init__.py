"""Checkout import shim for ad-hoc source commands.

The installable package lives under ``src/agent_decoder``. When a developer runs
``python -c 'from agent_decoder.cli import main; main()'`` from a git checkout,
this shim makes that import resolve to the checkout's source tree instead of a
different editable install already present in site-packages.
"""

from __future__ import annotations

from pathlib import Path

_SOURCE_PACKAGE = Path(__file__).resolve().parents[1] / "src" / "agent_decoder"
_SOURCE_INIT = _SOURCE_PACKAGE / "__init__.py"

if not _SOURCE_INIT.is_file():  # pragma: no cover - defensive import failure
    raise ModuleNotFoundError(f"Cannot find source package at {_SOURCE_PACKAGE}")

__path__ = [str(_SOURCE_PACKAGE)]
__file__ = str(_SOURCE_INIT)

exec(compile(_SOURCE_INIT.read_text(encoding="utf-8"), __file__, "exec"), globals())

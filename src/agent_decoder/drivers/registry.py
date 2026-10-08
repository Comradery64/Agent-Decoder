"""Driver registry — maps a provider to its live-driving backend.

Separate from ``providers/registry.py`` (which maps providers to *decode*
adapters). This one answers two questions the server needs:

  * ``availability_for(provider)`` — can we drive this provider on this box
    right now? Composes the backend probe (tmux present) with the provider's
    own CLI binary check (codex present). This is the runtime half of the
    effective-capability gate: ``effective_can_send = caps.can_send AND
    availability_for(provider).available``.
  * ``build_driver(...)`` — construct a ready-to-spawn ``ProviderDriver``.

v1 wires exactly one entry: ``codex → TmuxDriver(codex_spawn_builder)``. Claude
is intentionally absent — it stays on its tuned direct-PTY path. The shape
supports a future ``claude → TmuxDriver(claude_spawn_builder)`` opt-in with no
rework.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass

from .base import DriverAvailability, ProviderDriver
from .tmux_driver import TmuxDriver, codex_spawn_builder


@dataclass(frozen=True)
class _Backend:
    """How to drive one provider: which driver class, spawn argv, and the
    extra CLI binary that must be present beyond the backend's own."""

    driver_cls: type[ProviderDriver]
    spawn_builder: Callable[..., list[str]]
    required_bin: str  # the provider CLI that must be on PATH


# Provider name → backend wiring. v1: codex only.
_BACKENDS: dict[str, _Backend] = {
    "codex": _Backend(
        driver_cls=TmuxDriver,
        spawn_builder=codex_spawn_builder,
        required_bin="codex",
    ),
}


def supports_driving(provider: str) -> bool:
    """True if a driving backend is *wired* for this provider (ignores runtime).

    Distinct from availability: Claude returns False here (no backend wired),
    while Codex returns True even on a box without tmux — use
    ``availability_for`` for the runtime gate.
    """
    return provider in _BACKENDS


def availability_for(provider: str) -> DriverAvailability:
    """Runtime drivability for ``provider`` = backend usable AND CLI present."""
    backend = _BACKENDS.get(provider)
    if backend is None:
        return DriverAvailability(
            available=False, reason=f"no live-driving backend for {provider!r}"
        )
    backend_avail = backend.driver_cls.availability()
    if not backend_avail.available:
        return backend_avail
    if shutil.which(backend.required_bin) is None:
        return DriverAvailability(
            available=False,
            reason=(
                f"live driving unavailable: {backend.required_bin} not found on PATH"
            ),
        )
    return DriverAvailability(available=True, reason=None)


def build_driver(
    provider: str,
    session_id: str,
    cwd: str,
    *,
    model: str | None = None,
    resume_uuid: str | None = None,
    fresh: bool = False,
    **driver_kwargs,
) -> ProviderDriver:
    """Construct a ``ProviderDriver`` for ``provider`` (not yet spawned).

    For Codex the resume UUID *is* ``Session.id``, so it defaults to
    ``session_id`` — the v1 happy path turns a viewed session continuable with
    no caller bookkeeping.

    Pass ``fresh=True`` for a brand-new chat: the CLI is spawned with NO resume
    target (codex mints its own rollout UUID on the first message; we adopt it
    afterward). ``fresh`` is explicit because ``resume_uuid=None`` alone is
    ambiguous — it falls back to ``session_id`` for the resume happy path.
    """
    backend = _BACKENDS.get(provider)
    if backend is None:
        raise KeyError(f"no live-driving backend registered for {provider!r}")
    if fresh:
        effective_resume: str | None = None
    elif resume_uuid is not None:
        effective_resume = resume_uuid
    else:
        effective_resume = session_id
    spawn_command = backend.spawn_builder(
        resume_uuid=effective_resume,
        model=model,
    )
    return backend.driver_cls(session_id, cwd, spawn_command, **driver_kwargs)

"""Live-driving backends for provider CLIs.

See ``base.ProviderDriver`` for the seam and ``tmux_driver.TmuxDriver`` for the
v1 POSIX backend. ``registry`` wires providers to backends and answers runtime
drivability.
"""

from __future__ import annotations

from .base import DriverAvailability, DriverState, ProviderDriver
from .registry import (
    availability_for,
    build_driver,
    supports_driving,
)
from .tmux_driver import TmuxDriver, codex_spawn_builder

__all__ = [
    "DriverAvailability",
    "DriverState",
    "ProviderDriver",
    "TmuxDriver",
    "codex_spawn_builder",
    "availability_for",
    "build_driver",
    "supports_driving",
]

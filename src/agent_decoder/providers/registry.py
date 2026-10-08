"""Provider registry — runtime catalogue of registered ``ProviderAdapter`` instances.

Registration is explicit (call ``register(adapter)`` at import time or in an
app init hook).  The registry is a plain module-level ordered dict so there
are no hidden singletons and tests can ``clear()`` between cases.

Typical usage::

    from agent_decoder.providers import registry
    from agent_decoder.providers.claude import ClaudeAdapter

    registry.register(ClaudeAdapter())

    # Dispatch by name:
    adapter = registry.get("claude")

    # Dispatch by file path (first adapter whose owns_path returns True):
    adapter = registry.adapter_for_path(Path("/home/user/.claude/projects/…/sess.jsonl"))
"""

from __future__ import annotations

from pathlib import Path

from .base import ProviderAdapter

# Ordered dict keyed by adapter.name.  Insertion order is preserved (Python
# 3.7+) and exposed via all_adapters().
_registry: dict[str, ProviderAdapter] = {}


def register(adapter: ProviderAdapter) -> None:
    """Register *adapter* under ``adapter.name``, replacing any prior entry.

    Args:
        adapter: A concrete ``ProviderAdapter`` instance.
    """
    _registry[adapter.name] = adapter


def get(name: str) -> ProviderAdapter:
    """Return the adapter registered under *name*.

    Args:
        name: The short provider identifier (e.g. ``"claude"``).

    Raises:
        KeyError: If no adapter with that name has been registered.
    """
    if name not in _registry:
        registered = list(_registry.keys())
        raise KeyError(
            f"No provider adapter registered for {name!r}. "
            f"Registered providers: {registered}"
        )
    return _registry[name]


def all_adapters() -> list[ProviderAdapter]:
    """Return all registered adapters in insertion order."""
    return list(_registry.values())


def adapter_for_path(path: Path) -> ProviderAdapter | None:
    """Return the first adapter that claims ownership of *path*, or ``None``.

    Iterates in insertion order so registration order determines priority when
    multiple adapters could theoretically match the same path.

    Args:
        path: Absolute path to a candidate session file.

    Returns:
        The first ``ProviderAdapter`` whose ``owns_path(path)`` returns True,
        or ``None`` if no registered adapter claims it.
    """
    for adapter in _registry.values():
        if adapter.owns_path(path):
            return adapter
    return None


def clear() -> None:
    """Remove all registered adapters.

    Intended for use in test teardown so registry state does not leak between
    test cases.
    """
    _registry.clear()

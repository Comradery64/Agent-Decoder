"""Provider abstraction layer — adapters bridge external AI tools into clau-decode.

Each supported tool (Claude Code, Codex, etc.) ships as a ``ProviderAdapter``
subclass registered via ``providers.registry``.  The server and scanner never
import concrete adapter modules directly; they work against the abstract seam
defined in ``providers.base``.
"""

from . import (
    registry,
)  # re-exported for convenience: ``from .providers import registry``


def register_builtins() -> None:
    """Register all built-in provider adapters (idempotent).

    Call this once near application startup (e.g. inside ``create_app`` or
    ``_do_scan``) before iterating ``registry.all_adapters()``.  Registering
    the same adapter name twice simply overwrites the entry, so it is safe to
    call multiple times.

    Import is deferred inside the function body to avoid import-time side
    effects and circular imports — concrete adapter modules may themselves
    import from ``..models`` or ``..scanner``.
    """
    from .claude import ClaudeAdapter
    from .codex import CodexAdapter

    registry.register(ClaudeAdapter())
    registry.register(CodexAdapter())

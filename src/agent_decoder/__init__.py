# THE single source of truth for Clau-Decode's version. Edit it here and
# nowhere else — every other place derives from this one string:
#   * Python packaging  — pyproject's [tool.hatch.version] reads this file.
#   * CLI `--version`   — cli.py imports __version__.
#   * HTTP API          — /api/host-info returns it.
#   * Settings ▸ About  — the frontend fetches it from that API (no copy in JS).
# Bump this on every release (and tag the commit `vX.Y.Z` to match).
__version__ = "0.3.2"

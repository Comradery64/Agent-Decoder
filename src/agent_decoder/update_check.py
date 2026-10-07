"""PyPI-based update check for the About panel's "new version" banner.

PyPI, not GitHub, is the version-check source: PyPI's JSON API needs no
authentication and has a far higher unauthenticated rate limit than GitHub's
REST API (60 requests/hour/IP) — a background check running on every machine
that has Clau-Decode installed shouldn't rely on a budget that low.
"""

from __future__ import annotations

import httpx
from packaging.version import InvalidVersion, Version

PYPI_JSON_URL_PREFIX = "https://pypi.org/pypi/"
CHECK_TIMEOUT_S = 5.0

# PyPI's project name, NOT the CLI command or Python package name (both stay
# "clau-decode"/"agent_decoder") — PyPI rejected "clau-decode" as too similar
# to an existing project, so the published distribution is "agent-decoder".
DEFAULT_PACKAGE = "agent-decoder"


async def fetch_latest_version(
    package: str = DEFAULT_PACKAGE, timeout: float = CHECK_TIMEOUT_S
) -> str | None:
    """Best-effort: the latest version string published on PyPI, or None.

    Never raises — offline, PyPI being down, or the package not yet being
    published are all normal conditions here, and should just mean "no
    update banner", not a surfaced error.
    """
    url = PYPI_JSON_URL_PREFIX + package + "/json"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()["info"]["version"]
    except Exception:
        return None


def is_newer(latest: str, current: str) -> bool:
    """True if `latest` is a strictly newer PEP 440 version than `current`.

    Fails closed on an unparsable version string (never show a banner we
    can't actually justify).
    """
    try:
        return Version(latest) > Version(current)
    except InvalidVersion:
        return False

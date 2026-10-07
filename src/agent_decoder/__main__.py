"""Module entry point so ``python -m agent_decoder`` runs the app.

Mirrors the ``clau-decode`` console script (``agent_decoder.cli:main``). Handy for
running straight from a checkout (``uv run python -m agent_decoder``) without a
global install.
"""

from .cli import main

if __name__ == "__main__":
    main()

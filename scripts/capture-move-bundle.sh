#!/usr/bin/env bash
#
# capture-move-bundle.sh — thin shim.
#
# Capture now lives inside the migrate tool (single source of truth), so the move
# is one tool with one self-driving entry point. This just delegates to it:
#
#   bash scripts/capture-move-bundle.sh [--bundle DIR]
#
# Equivalent to:  python3 src/agent_decoder/migrate.py --capture [...]
# Or, the fully guided one-command flow (auto-detects capture vs merge):
#   clau-decode migrate      # installed
#   python3 src/agent_decoder/migrate.py   # install-free
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/../src/agent_decoder/migrate.py" --capture "$@"

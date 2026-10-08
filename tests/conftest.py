"""Pytest configuration for tests."""

import sys
from pathlib import Path

# Add src directory to path so tests can import agent_decoder
src_dir = Path(__file__).parent.parent / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

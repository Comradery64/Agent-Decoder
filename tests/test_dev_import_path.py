from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_repo_root_python_prefers_checkout_src_without_pythonpath():
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "import agent_decoder; "
                "print(Path(agent_decoder.__file__).resolve())"
            ),
        ],
        cwd=repo_root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    imported = Path(result.stdout.strip())
    assert imported == repo_root / "src" / "agent_decoder" / "__init__.py"

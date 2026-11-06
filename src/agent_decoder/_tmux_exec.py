"""Consume a private environment handoff then exec a persistent harness.

Environment values never appear in tmux's argv or durable session metadata.
The handoff lives in a mode-0700 temporary directory and is unlinked before
executing the provider. This process becomes the pane PID via exec.
"""

import json
import os
import sys

with open(sys.argv[1]) as handoff:
    payload = json.load(handoff)
os.unlink(sys.argv[1])
os.rmdir(os.path.dirname(sys.argv[1]))
os.execvpe(payload["argv"][0], payload["argv"], payload["env"])

"""PTY controlling-TTY attach wrapper.

uvloop's subprocess implementation does not run ``preexec_fn`` for safety
reasons (libuv constraint). The PTY runner needs ``setsid()`` +
``TIOCSCTTY`` on the slave fd before the spawned ``claude`` binary's main()
runs, or the TUI will fail to render.

Solution: invoke this wrapper as the entry point. It performs the
controlling-TTY claim in the *child* process (after Python's subprocess
infrastructure has dup2'd the slave fd into fd 0/1/2), then ``execvp`` into
the real target binary. From here on the child is fully detached and runs
under its own session with the slave as its controlling TTY.

Usage:
    python3 -m agent_decoder._pty_preexec <bin_name> [args...]

The first argv after the module name is the target binary; remaining args
are forwarded.
"""

from __future__ import annotations

import fcntl
import os
import sys
import termios


def main() -> None:
    if len(sys.argv) < 2:
        sys.stderr.write("usage: _pty_preexec <binary> [args...]\n")
        raise SystemExit(2)

    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    target = sys.argv[1]
    os.execvp(target, sys.argv[1:])


if __name__ == "__main__":
    main()

"""Persistent Claude terminal identity, independent of the attached client.

The session option identifies terminals created by this application. It is a
coordination boundary between same-user processes, not an authentication token.
A fresh foreign sidecar must still prevent adoption of a managed terminal.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
import socket
import shlex
import struct
import uuid
from pathlib import Path


class TmuxTerminal:
    def __init__(
        self, session_id: str, cwd: str, *, socket_name: str = "agent-decoder"
    ):
        self.session_id = session_id
        self.cwd = os.path.realpath(cwd)
        self.socket_name = socket_name
        self._capture_socket = None
        self._capture_dir = None
        self._capture_seq = 0
        self._capture_owner = uuid.uuid4().hex
        # Hashing avoids collisions from replacing forbidden name characters.
        self.name = "claude_" + hashlib.sha256(session_id.encode()).hexdigest()[:32]

    @staticmethod
    def available() -> bool:
        return shutil.which("tmux") is not None

    def argv(self, *args: str) -> list[str]:
        return ["tmux", "-L", self.socket_name, *args]

    async def run(self, *args: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            *self.argv(*args),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode:
            raise RuntimeError(
                f"tmux {args[0]} failed: {err.decode(errors='replace').strip()}"
            )
        return out.decode().strip()

    async def exists(self) -> bool:
        try:
            await self.run("has-session", "-t", self.name)
            return True
        except RuntimeError:
            return False

    def identity(self) -> str:
        return json.dumps(
            {
                "version": 1,
                "provider": "claude",
                "session_id": self.session_id,
                "cwd": self.cwd,
            },
            sort_keys=True,
        )

    async def _process_identity(self) -> dict:
        panes = await self.run(
            "list-panes", "-s", "-t", self.name, "-F", "#{pane_pid}:#{pane_dead}"
        )
        if len(panes.splitlines()) != 1:
            raise RuntimeError("managed tmux terminal must contain exactly one pane")
        pid, dead = panes.split(":")
        if dead != "0" or int(pid) <= 0:
            raise RuntimeError("managed tmux pane is no longer live")
        proc = await asyncio.create_subprocess_exec(
            "ps",
            "-p",
            pid,
            "-o",
            "lstart=",
            "-o",
            "comm=",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        if proc.returncode or not out.strip():
            raise RuntimeError("cannot verify managed pane process")
        return {"pid": int(pid), "process": out.decode().strip()}

    @staticmethod
    def launch_config(argv: list[str]) -> list[str]:
        # Initial session creation and subsequent --resume are equivalent.
        config = list(argv)
        for flag in ("--session-id", "--resume"):
            if flag in config:
                index = config.index(flag)
                del config[index : index + 2]
        return config

    async def managed_pid(self) -> int | None:
        if not await self.exists():
            return None
        identity = await self.run(
            "show-options", "-v", "-t", self.name, "@agent-decoder-identity"
        )
        if identity != self.identity():
            raise RuntimeError(
                "refusing to adopt tmux session with mismatched managed identity"
            )
        saved = json.loads(
            await self.run(
                "show-options", "-v", "-t", self.name, "@agent-decoder-process"
            )
        )
        current = await self._process_identity()
        if current != saved:
            raise RuntimeError(
                "managed pane process changed; refusing ownership adoption"
            )
        return current["pid"]

    async def ensure(
        self, argv: list[str], env: dict[str, str], rows: int, cols: int
    ) -> int:
        pid = await self.managed_pid()
        if pid is not None:
            config = json.loads(
                await self.run(
                    "show-options", "-v", "-t", self.name, "@agent-decoder-launch"
                )
            )
            if config == self.launch_config(argv):
                return pid
            # Caller acquired the ownership sidecar before reaching this point.
            # A changed binary/permission/model needs a fresh provider process.
            await self.terminate()
        # Private file handoff avoids exposing inherited auth values in argv.
        with tempfile.TemporaryDirectory(prefix="agent-decoder-terminal-") as temp_dir:
            handoff = Path(temp_dir) / "launch.json"
            handoff.write_text(json.dumps({"argv": argv, "env": env}))
            handoff.chmod(0o600)
            await self.run(
                "new-session",
                "-d",
                "-s",
                self.name,
                "-x",
                str(cols),
                "-y",
                str(rows),
                "-c",
                self.cwd,
                "--",
                sys.executable,
                str(Path(__file__).with_name("_tmux_exec.py")),
                str(handoff),
            )
            try:
                # Wait for the handoff exec before recording the real harness.
                for _ in range(100):
                    if not handoff.exists():
                        break
                    await asyncio.sleep(0.01)
                else:
                    raise RuntimeError(
                        "provider did not consume tmux environment handoff"
                    )
                previous = None
                for _ in range(100):
                    process = await self._process_identity()
                    probe = await asyncio.create_subprocess_exec(
                        "ps",
                        "-p",
                        str(process["pid"]),
                        "-o",
                        "command=",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    command, _ = await probe.communicate()
                    if b"_tmux_exec.py" not in command and process == previous:
                        break
                    previous = process
                    await asyncio.sleep(0.05)
                else:
                    raise RuntimeError(
                        "provider process identity did not settle after exec"
                    )
                await self.run(
                    "set-option",
                    "-t",
                    self.name,
                    "@agent-decoder-identity",
                    self.identity(),
                )
                await self.run(
                    "set-option",
                    "-t",
                    self.name,
                    "@agent-decoder-process",
                    json.dumps(process),
                )
                await self.run(
                    "set-option",
                    "-t",
                    self.name,
                    "@agent-decoder-launch",
                    json.dumps(self.launch_config(argv)),
                )
                await self.run("set-option", "-t", self.name, "status", "off")
            except BaseException:
                await self.run("kill-session", "-t", self.name)
                raise
        return process["pid"]

    async def start_capture(self, on_chunk, on_gap) -> None:
        """Capture original pane bytes; attach output is a rendered screen."""
        self._capture_dir = tempfile.TemporaryDirectory(prefix="ad-capture-")
        path = str(Path(self._capture_dir.name) / "raw")
        sink = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sink.bind(path)
        sink.setblocking(False)
        os.chmod(path, 0o600)
        self._capture_socket = sink
        self._capture_seq = 0

        def receive():
            # Bound work per event-loop turn as well as bytes per datagram.
            for _ in range(64):
                try:
                    packet = sink.recv(2048)
                except BlockingIOError:
                    return
                if len(packet) < 8:
                    on_gap()
                    continue
                sequence = struct.unpack("!Q", packet[:8])[0]
                if sequence != self._capture_seq + 1:
                    on_gap()
                self._capture_seq = sequence
                on_chunk(packet[8:])

        asyncio.get_running_loop().add_reader(sink.fileno(), receive)
        command = shlex.join(
            [sys.executable, str(Path(__file__).with_name("_tmux_capture.py")), path]
        )
        try:
            # Replaces any orphaned relay after a crashed application.
            await self.run(
                "set-option",
                "-t",
                self.name,
                "@agent-decoder-capture-owner",
                self._capture_owner,
            )
            await self.run("pipe-pane", "-O", "-t", self.name, command)
        except BaseException:
            self.close_capture()
            raise

    def close_capture(self) -> None:
        sink = self._capture_socket
        self._capture_socket = None
        if sink is not None:
            asyncio.get_running_loop().remove_reader(sink.fileno())
            sink.close()
        if self._capture_dir is not None:
            self._capture_dir.cleanup()
            self._capture_dir = None

    async def stop_capture(self) -> None:
        self.close_capture()
        if await self.exists():
            await self.run(
                "if-shell",
                "-t",
                self.name,
                "-F",
                "#{==:#{@agent-decoder-capture-owner}," + self._capture_owner + "}",
                "pipe-pane -t " + self.name,
            )

    async def terminate(self) -> None:
        # Verify identity again before destructive operations.
        if await self.managed_pid() is not None:
            await self.run("kill-session", "-t", self.name)

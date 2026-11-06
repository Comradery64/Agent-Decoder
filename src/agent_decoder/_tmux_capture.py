"""Drain tmux pipe-pane without ever waiting for the application receiver.

Datagrams are bounded and sequenced. A stopped/full receiver drops data rather
than blocking the provider; the reader rejects incomplete ephemeral captures.
"""

import os
import socket
import struct
import sys

sink = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
sink.setblocking(False)
sequence = 0
while True:
    chunk = os.read(0, 1024)
    if not chunk:
        break
    sequence += 1
    try:
        sink.sendto(struct.pack("!Q", sequence) + chunk, sys.argv[1])
    except OSError:
        pass

"""Just enough of sway's IPC: commands (switching workspaces for the overlay)."""
import json
import socket
import struct

MAGIC = b"i3-ipc"
RUN_COMMAND = 0


class Sway:
    def __init__(self, path: str):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)

    def _send(self, kind: int, payload: str):
        data = payload.encode()
        self.sock.sendall(MAGIC + struct.pack("=II", len(data), kind) + data)

    def _read(self) -> tuple[int, object]:
        head = self._exact(14)
        size, kind = struct.unpack("=II", head[6:])
        return kind, json.loads(self._exact(size))

    def _exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("sway is gone")
            buf += chunk
        return buf

    def command(self, cmd: str):
        self._send(RUN_COMMAND, cmd)
        return self._read()[1]

"""Just enough of sway's IPC: commands, and the binding events our key bindings produce."""
import json
import socket
import struct

MAGIC = b"i3-ipc"
RUN_COMMAND, SUBSCRIBE = 0, 2
EVENT_BINDING = 0x80000005


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

    def bindings(self):
        """Yields the command of every binding that runs ("nop alia press button east")."""
        self._send(SUBSCRIBE, '["binding"]')
        self._read()
        while True:
            kind, ev = self._read()
            if kind == EVENT_BINDING and ev.get("change") == "run":
                yield ev["binding"]["command"]

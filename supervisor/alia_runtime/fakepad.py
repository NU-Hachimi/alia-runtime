"""The gamepad without /dev/uinput (Docker Desktop on Windows and macOS has none): the same
device as uinput.Gamepad, served to the cart through a device node the kernel doesn't
have and a preloaded library that answers for it (image/fakepad/fakepad.c). The cart's SDL
sees alia-inputd's gamepad all the same; its reports come from here, over a socket."""
import os
import socket
import struct
import threading
import time

from .uinput import AXES, BUTTONS, EV_ABS, EV_KEY, EV_SYN, SYN_REPORT, AXIS_MIN, AXIS_MAX

NODE = "/dev/input/event200"          # major 13 like real evdev nodes; no driver behind it
SOCKET = "/run/alia-rt/pad.sock"
LIBRARY = "/opt/alia-runtime/lib/libaliafakepad.so"


class FakeGamepad:
    def __init__(self, uid: int, gid: int):
        os.makedirs("/dev/input", exist_ok=True)
        if os.path.exists(NODE):
            os.unlink(NODE)
        os.mknod(NODE, 0o020666, os.makedev(13, 200))
        os.makedirs(os.path.dirname(SOCKET), exist_ok=True)
        if os.path.exists(SOCKET):
            os.unlink(SOCKET)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.server.bind(SOCKET)
        os.chown(SOCKET, uid, gid)
        os.chmod(SOCKET, 0o600)
        self.server.listen()
        self.clients: list = []
        self.lock = threading.Lock()
        self.buttons = {k: 0 for k in BUTTONS}
        self.axes = {k: 0 for k in AXES}
        threading.Thread(target=self._accept, daemon=True).start()

    def env(self) -> dict:
        """What the cart needs to see this gamepad (added to runtime.toml's environment)."""
        return {"LD_PRELOAD": LIBRARY, "ALIA_FAKEPAD": NODE, "ALIA_FAKEPAD_SOCKET": SOCKET}

    def _accept(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with self.lock:
                self.clients.append(conn)

    def _send(self, events: list):
        now = time.time()
        sec, usec = int(now), int((now % 1) * 1e6)
        packet = b"".join(struct.pack("qqHHi", sec, usec, t, c, v) for t, c, v in events)
        with self.lock:
            for conn in list(self.clients):
                try:
                    conn.send(packet, socket.MSG_DONTWAIT)
                except OSError:  # closed by the cart, or a cart that doesn't read: drop it
                    self.clients.remove(conn)
                    conn.close()

    def set(self, buttons: dict | None = None, axes: dict | None = None):
        events = []
        for k, v in (buttons or {}).items():
            if self.buttons[k] != int(v):
                self.buttons[k] = int(v)
                events.append((EV_KEY, BUTTONS[k], int(v)))
        for k, v in (axes or {}).items():
            v = max(AXIS_MIN, min(AXIS_MAX, int(v)))
            if self.axes[k] != v:
                self.axes[k] = v
                events.append((EV_ABS, AXES[k], v))
        if events:
            self._send(events + [(EV_SYN, SYN_REPORT, 0)])

    def close(self):
        self.server.close()
        with self.lock:
            for conn in self.clients:
                conn.close()
            self.clients.clear()

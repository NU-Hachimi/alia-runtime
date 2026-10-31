"""The keyboard, key by key: a minimal Wayland client (no libraries) that puts an invisible
1x1 surface on sway's overlay layer with exclusive keyboard focus, and reports every key's
own press and release.

Why not sway's key bindings: a binding matches the whole set of held keys, and a release
binding fires only for the key pressed last. Hold W (walk) and tap X (A), and W's release
never came: the stick stayed up. A gamepad on a keyboard needs each key on its own.

Keys are reported by name, through the keymap the compositor sends (libxkbcommon): the
key code -> its unshifted symbol -> the SDL3 name runtime.toml [keyboard] uses ("W",
"Return", "Left Shift"), the same meaning SDL gives them in the engine's direct runs.
"""
import ctypes
import mmap
import os
import socket
import struct

# xkb symbol name (unshifted) -> SDL3 key name, where they differ; letters: "w" -> "W"
SDL_NAMES = {"Return": "Return", "KP_Enter": "Keypad Enter", "equal": "=", "minus": "-",
             "BackSpace": "Backspace", "Shift_L": "Left Shift", "Shift_R": "Right Shift",
             "space": "Space", "Escape": "Escape", "Tab": "Tab"}


def sdl_name(xkb: str) -> str:
    return SDL_NAMES.get(xkb, xkb.upper() if len(xkb) == 1 else xkb)


class Keymap:
    """libxkbcommon through ctypes (sway depends on it): key code -> unshifted symbol name."""

    def __init__(self):
        x = self.x = ctypes.CDLL("libxkbcommon.so.0")
        x.xkb_context_new.restype = ctypes.c_void_p
        x.xkb_keymap_new_from_string.restype = ctypes.c_void_p
        x.xkb_keymap_new_from_string.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
        x.xkb_state_new.restype = ctypes.c_void_p
        x.xkb_state_new.argtypes = [ctypes.c_void_p]
        x.xkb_state_key_get_one_sym.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        x.xkb_state_key_get_one_sym.restype = ctypes.c_uint32
        x.xkb_keysym_get_name.argtypes = [ctypes.c_uint32, ctypes.c_char_p, ctypes.c_size_t]
        self.ctx = x.xkb_context_new(0)
        self.state = None

    def load(self, text: bytes):
        keymap = self.x.xkb_keymap_new_from_string(self.ctx, text, 1, 0)  # XKB_KEYMAP_FORMAT_TEXT_V1
        if keymap:
            self.state = self.x.xkb_state_new(keymap)  # never fed modifiers: always the base level

    def name(self, code: int) -> str | None:
        if not self.state:
            return None
        sym = self.x.xkb_state_key_get_one_sym(self.state, code + 8)  # evdev code + 8 = xkb keycode
        buf = ctypes.create_string_buffer(64)
        if not sym or self.x.xkb_keysym_get_name(sym, buf, 64) <= 0:
            return None
        return sdl_name(buf.value.decode())


WL_DISPLAY = 1
LAYER_OVERLAY, KEYBOARD_EXCLUSIVE, ANCHOR_TOP_LEFT = 3, 1, 1 | 4


class Wayland:
    def __init__(self, path: str):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.next_id = 2
        self.handlers = {}   # object id -> function(opcode, payload, fds)
        self.buf, self.fds = b"", []

    def new(self, handler=None) -> int:
        oid = self.next_id
        self.next_id += 1
        if handler:
            self.handlers[oid] = handler
        return oid

    def send(self, oid: int, opcode: int, args: bytes = b"", fds=()):
        msg = struct.pack("<IHH", oid, opcode, 8 + len(args)) + args
        if fds:
            socket.send_fds(self.sock, [msg], list(fds))
        else:
            self.sock.sendall(msg)

    @staticmethod
    def string(s: str) -> bytes:
        b = s.encode() + b"\0"
        return struct.pack("<I", len(b)) + b + b"\0" * (-len(b) % 4)

    def dispatch(self):
        """Read from the socket and call the handlers; raises ConnectionError when sway goes."""
        data, fds, _flags, _addr = socket.recv_fds(self.sock, 65536, 8)
        if not data:
            raise ConnectionError("the compositor is gone")
        self.buf += data
        self.fds += fds
        while len(self.buf) >= 8:
            oid, opcode, size = struct.unpack_from("<IHH", self.buf)
            if len(self.buf) < size:
                break
            payload, self.buf = self.buf[8:size], self.buf[size:]
            handler = self.handlers.get(oid)
            if handler:
                handler(opcode, payload)


def run(path: str, on_key):
    """on_key(name, pressed) for every key (SDL3 names), until the compositor goes
    (ConnectionError)."""
    wl = Wayland(path)
    keymap = Keymap()
    globals_ = {}

    def registry_event(op, p):
        if op == 0:  # global(name, interface, version)
            name, n = struct.unpack_from("<II", p)
            iface = p[8:8 + n - 1].decode()
            version, = struct.unpack_from("<I", p, 8 + n + (-n % 4))
            globals_[iface] = (name, version)

    def bind(iface: str, version: int, handler=None) -> int:
        name, have = globals_[iface]
        oid = wl.new(handler)
        wl.send(registry, 0, struct.pack("<I", name) + wl.string(iface) + struct.pack("<II", min(version, have), oid))
        return oid

    def roundtrip():
        done = []
        cb = wl.new(lambda op, p: done.append(1))
        wl.send(WL_DISPLAY, 0, struct.pack("<I", cb))  # wl_display.sync
        while not done:
            wl.dispatch()

    def display_event(op, p):
        if op == 0:  # error(object, code, message)
            raise RuntimeError("wayland error: " + p[12:].split(b"\0")[0].decode(errors="replace"))

    wl.handlers[WL_DISPLAY] = display_event
    registry = wl.new(registry_event)
    wl.send(WL_DISPLAY, 1, struct.pack("<I", registry))  # wl_display.get_registry
    roundtrip()
    for need in ("wl_compositor", "wl_shm", "wl_seat", "zwlr_layer_shell_v1"):
        if need not in globals_:
            raise RuntimeError(f"the compositor has no {need}")

    compositor = bind("wl_compositor", 4)
    shm = bind("wl_shm", 1)
    held: dict[int, str | None] = {}  # key code -> the name it had when pressed

    def keyboard_event(op, p):
        if op == 0:      # keymap(format, fd, size)
            fmt, size = struct.unpack_from("<II", p)[0], struct.unpack_from("<I", p, 4)[0]
            for fd in wl.fds:
                if fmt == 1:  # xkb v1
                    with mmap.mmap(fd, size, prot=mmap.PROT_READ) as m:
                        keymap.load(m[:].rstrip(b"\0") + b"\0")
                os.close(fd)
            wl.fds.clear()
        elif op == 1:    # enter(serial, surface, keys[]): keys already down count as pressed
            n, = struct.unpack_from("<I", p, 8)
            for code in struct.unpack_from(f"<{n // 4}I", p, 12):
                if code not in held:
                    held[code] = keymap.name(code)
                    on_key(held[code], True)
        elif op == 2:    # leave: everything up
            for name in held.values():
                on_key(name, False)
            held.clear()
        elif op == 3:    # key(serial, time, key, state); a release goes by what the press was
            _serial, _time, code, state = struct.unpack_from("<IIII", p)
            if state and code not in held:
                held[code] = keymap.name(code)
                on_key(held[code], True)
            elif not state and code in held:
                on_key(held.pop(code), False)

    def seat_event(op, p):
        if op == 0 and struct.unpack_from("<I", p)[0] & 2 and not keyboards:  # capabilities: keyboard
            kb = wl.new(keyboard_event)
            keyboards.append(kb)
            wl.send(seat, 1, struct.pack("<I", kb))  # wl_seat.get_keyboard

    keyboards: list[int] = []
    seat = bind("wl_seat", 5, seat_event)
    layer_shell = bind("zwlr_layer_shell_v1", 1)

    # a 1x1 transparent buffer: a layer surface must have one to be mapped
    fd = os.memfd_create("alia-keys")
    os.ftruncate(fd, 4)
    mmap.mmap(fd, 4)[:] = b"\0\0\0\0"
    pool = wl.new()
    wl.send(shm, 0, struct.pack("<II", pool, 4), fds=[fd])  # create_pool(id, fd, size)
    os.close(fd)
    buffer = wl.new()
    wl.send(pool, 0, struct.pack("<IiiiiI", buffer, 0, 1, 1, 4, 0))  # create_buffer, ARGB8888

    surface = wl.new()
    wl.send(compositor, 0, struct.pack("<I", surface))
    region = wl.new()
    wl.send(compositor, 1, struct.pack("<I", region))           # an empty region: no pointer input
    wl.send(surface, 5, struct.pack("<I", region))              # set_input_region

    configured = []

    def layer_event(op, p):
        if op == 0:      # configure(serial, w, h)
            serial, = struct.unpack_from("<I", p)
            wl.send(layer, 6, struct.pack("<I", serial))         # ack_configure
            if not configured:
                wl.send(surface, 1, struct.pack("<Iii", buffer, 0, 0))  # attach
                wl.send(surface, 6)                                  # commit
            configured.append(serial)

    layer = wl.new(layer_event)
    wl.send(layer_shell, 0, struct.pack("<III", layer, surface, 0) + struct.pack("<I", LAYER_OVERLAY)
            + wl.string("alia-keys"))
    wl.send(layer, 0, struct.pack("<II", 1, 1))                         # set_size
    wl.send(layer, 1, struct.pack("<I", ANCHOR_TOP_LEFT))               # set_anchor
    wl.send(layer, 4, struct.pack("<I", KEYBOARD_EXCLUSIVE))            # set_keyboard_interactivity
    wl.send(surface, 6)                                                 # commit (no buffer yet)
    while True:
        wl.dispatch()

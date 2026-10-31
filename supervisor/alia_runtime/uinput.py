"""Virtual gamepads through /dev/uinput, the same device the handheld's alia-inputd creates
(name, IDs, buttons and axes), so the SDL mapping in alia-sdk's runtime.toml applies as is."""
import fcntl
import os
import struct

EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
SYN_REPORT = 0
# the buttons alia-inputd declares (daemons/inputd/src/uinput.cpp key_code); SDL numbers them
# in code order, which the runtime.toml mapping relies on
BUTTONS = {"south": 0x130, "east": 0x131, "north": 0x133, "west": 0x134, "l1": 0x136, "r1": 0x137,
           "l2": 0x138, "r2": 0x139, "select": 0x13a, "start": 0x13b, "l3": 0x13d, "r3": 0x13e,
           "up": 0x220, "down": 0x221, "left": 0x222, "right": 0x223}
AXES = {"lx": 0x00, "ly": 0x01, "rx": 0x03, "ry": 0x04}
AXIS_MIN, AXIS_MAX, AXIS_FUZZ = -32768, 32767, 256


def _ioc(direction, nr, size):
    return (direction << 30) | (size << 16) | (ord("U") << 8) | nr


UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_ABSBIT = _ioc(1, 100, 4), _ioc(1, 101, 4), _ioc(1, 103, 4)
UI_DEV_SETUP, UI_ABS_SETUP = _ioc(1, 3, 92), _ioc(1, 4, 28)
UI_DEV_CREATE, UI_DEV_DESTROY = _ioc(0, 1, 0), _ioc(0, 2, 0)


def UI_GET_SYSNAME(n):
    return _ioc(2, 44, n)


class Gamepad:
    def __init__(self, name: str, bustype: int, vendor: int, product: int, version: int):
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_ABS)
        for code in BUTTONS.values():
            fcntl.ioctl(self.fd, UI_SET_KEYBIT, code)
        for code in AXES.values():
            fcntl.ioctl(self.fd, UI_SET_ABSBIT, code)
            # struct uinput_abs_setup: u16 code, pad, struct input_absinfo (value min max fuzz flat res)
            fcntl.ioctl(self.fd, UI_ABS_SETUP, struct.pack("Hxxiiiiii", code, 0, AXIS_MIN, AXIS_MAX, AXIS_FUZZ, 0, 0))
        # struct uinput_setup: struct input_id, char name[80], u32 ff_effects_max
        fcntl.ioctl(self.fd, UI_DEV_SETUP, struct.pack("HHHH80sI", bustype, vendor, product, version, name.encode(), 0))
        fcntl.ioctl(self.fd, UI_DEV_CREATE)
        self.buttons = {k: 0 for k in BUTTONS}
        self.axes = {k: 0 for k in AXES}

    def sysname(self) -> str:
        """input<N> under /sys/devices/virtual/input: where to find the event node."""
        buf = bytearray(64)
        fcntl.ioctl(self.fd, UI_GET_SYSNAME(len(buf)), buf)
        return buf.split(b"\0")[0].decode()

    def _event(self, kind, code, value):
        os.write(self.fd, struct.pack("qqHHi", 0, 0, kind, code, value))

    def set(self, buttons: dict | None = None, axes: dict | None = None):
        """Latest state; emits only what changed, then one SYN_REPORT."""
        changed = False
        for k, v in (buttons or {}).items():
            if self.buttons[k] != int(v):
                self.buttons[k] = int(v)
                self._event(EV_KEY, BUTTONS[k], int(v))
                changed = True
        for k, v in (axes or {}).items():
            v = max(AXIS_MIN, min(AXIS_MAX, int(v)))
            if self.axes[k] != v:
                self.axes[k] = v
                self._event(EV_ABS, AXES[k], v)
                changed = True
        if changed:
            self._event(EV_SYN, SYN_REPORT, 0)

    def close(self):
        if self.fd >= 0:
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
            os.close(self.fd)
            self.fd = -1

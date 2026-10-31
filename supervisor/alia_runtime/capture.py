"""The screen as pictures, for the browser mode: sway's screencopy (wlr-screencopy-unstable-v1)
through wlkeys' minimal Wayland client, frame after frame, only when something changed.
Each picture is handed over as a Pillow image."""
import mmap
import os
import struct
import time

from PIL import Image

from .wlkeys import Wayland

# wl_shm formats we read -> Pillow's raw mode for them (little-endian bytes). sway offers
# XRGB with software rendering and XBGR with the GPU.
RAW = {0: "BGRX", 1: "BGRX",                      # ARGB8888, XRGB8888
       0x34324241: "RGBX", 0x34324258: "RGBX"}    # ABGR8888, XBGR8888


class Capture:
    def __init__(self, path: str):
        self.wl = wl = Wayland(path)
        self.globals = {}
        registry = wl.new(self._global)
        wl.send(1, 1, struct.pack("<I", registry))
        self._roundtrip()
        for need in ("wl_shm", "wl_output", "zwlr_screencopy_manager_v1"):
            if need not in self.globals:
                raise RuntimeError(f"the compositor has no {need}")
        self.registry = registry
        self.shm = self._bind("wl_shm", 1)
        self.output = self._bind("wl_output", 1)
        self.manager = self._bind("zwlr_screencopy_manager_v1", 3)
        self.buffer = None  # (wl_buffer id, mmap, w, h, stride, format)

    def _global(self, op, p):
        if op == 0:
            name, n = struct.unpack_from("<II", p)
            iface = p[8:8 + n - 1].decode()
            version, = struct.unpack_from("<I", p, 8 + n + (-n % 4))
            self.globals[iface] = (name, version)

    def _bind(self, iface, version):
        name, have = self.globals[iface]
        oid = self.wl.new()
        self.wl.send(self.registry, 0, struct.pack("<I", name) + self.wl.string(iface)
                     + struct.pack("<II", min(version, have), oid))
        return oid

    def _roundtrip(self):
        done = []
        cb = self.wl.new(lambda op, p: done.append(1))
        self.wl.send(1, 0, struct.pack("<I", cb))
        while not done:
            self.wl.dispatch()

    def _make_buffer(self, fmt, w, h, stride):
        fd = os.memfd_create("alia-capture")
        os.ftruncate(fd, stride * h)
        data = mmap.mmap(fd, stride * h)
        pool = self.wl.new()
        self.wl.send(self.shm, 0, struct.pack("<II", pool, stride * h), fds=[fd])
        os.close(fd)
        buf = self.wl.new()
        self.wl.send(pool, 0, struct.pack("<IiiiiI", buf, 0, w, h, stride, fmt))
        self.buffer = (buf, data, w, h, stride, fmt)

    def grab(self, timeout: float = 1.0):
        """One picture of the output (PIL.Image, RGB), or None if the compositor failed it."""
        st = {"info": None, "ready": False, "failed": False, "done": False}

        def frame_event(op, p):
            if op == 0:    # buffer(format, width, height, stride): a wl_shm buffer that fits
                fmt, w, h, stride = struct.unpack_from("<IIII", p)
                if fmt in RAW and st["info"] is None:
                    st["info"] = (fmt, w, h, stride)
            elif op == 6:  # buffer_done: every offer is in; copy into ours
                st["done"] = True
            elif op == 2:  # ready
                st["ready"] = True
            elif op == 3:  # failed
                st["failed"] = True

        frame = self.wl.new(frame_event)
        self.wl.send(self.manager, 0, struct.pack("<IiI", frame, 0, self.output))  # capture_output, no cursor
        deadline = time.monotonic() + timeout
        while not st["done"] and not st["failed"] and time.monotonic() < deadline:
            self.wl.dispatch(timeout=0.05)
        if st["info"] is None or st["failed"]:
            self.wl.send(frame, 1)
            return None
        fmt, w, h, stride = st["info"]
        if not self.buffer or self.buffer[2:] != (w, h, stride, fmt):
            self._make_buffer(fmt, w, h, stride)
        self.wl.send(frame, 0, struct.pack("<I", self.buffer[0]))  # copy
        while not st["ready"] and not st["failed"] and time.monotonic() < deadline:
            self.wl.dispatch(timeout=0.05)
        self.wl.send(frame, 1)                                       # destroy
        if not st["ready"]:
            return None
        _buf, data, w, h, stride, fmt = self.buffer
        return Image.frombuffer("RGB", (w, h), bytes(data), "raw", RAW[fmt], stride, 1)

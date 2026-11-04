"""The handheld's camera on the PC (docs/design.md §8.1; the handheld's C8). A cart whose manifest
declares [permissions] camera finds the camera socket where the handheld puts it
(/run/alia/camera.sock) and gets protocol v2 (the devkit's include/alia/camera_proto.h): modes,
buffers (memfds: mmap them, or import them where the GPU can), PARAMS, frames, pause/resume,
photos, typed errors. What's in the frames (ALIA_RT_CAMERA):
  clip     (default) a short recording from the handheld's camera (camera_clip.zip: 8 s at 15 fps,
           1024x768 JPEG; a placeholder until a raw IMX219 clip), looped
  scene    a test scene: colour patches, grey ramp, a moving disc, a frame counter (deterministic:
           the tests use it)
  webcam   this PC's camera: on Linux the device acart passes in (v4l2-ctl), in the browser
           mode the page's (it asks the browser when a cart opens the camera)
  none     no camera: OPEN answers UNAVAILABLE, as a handheld without one
Frames are made like the handheld's sensor would see that picture: the cart's colour pipeline run
backwards (the IMX219's tuning at a 5000 K light, exported from alia-pad's isp_params:
camera_imx219.json), black level, noise, Bayer (BGGR). Developed with the reference shader they
come out as the picture. ALIA_RT_CAMERA_PERMISSION: allow (default) | ask (the supervisor asks,
the cart frozen meanwhile, as the handheld's prompt) | deny.

A process of its own (numpy, Pillow: the supervisor's environment, not the carts'). Talks to the
supervisor over stdin/stdout lines: in "suspend", "resume", "permission granted|denied"; out
"ask", "streaming", "off".
"""
from __future__ import annotations

import json
import logging
import math
import mmap
import os
import selectors
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

logging.basicConfig(level=logging.INFO, format="alia-runtime camera: %(message)s", stream=sys.stderr)
log = logging.getLogger("camera")

SOCKET = "/run/alia/camera.sock"
FEED = "/run/alia-rt/camera-feed.sock"     # the browser page's webcam (web.py connects)
DATA = json.loads(Path(__file__).with_name("camera_imx219.json").read_text())
LIGHT = "5000"                             # the simulated light
ORDER, RPOS = b"BGGR", (1, 1)              # as the handheld's (the sensor flipped for rotation 180)
NBUF, MAX_HELD, BLACK = 6, 2, int(DATA["black_level"])
EXPOSURE = 0.8                             # scene white -> 80 % of the sensor's range (headroom)
FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"

HELLO, OPEN, RELEASE, CLOSE, PHOTO = 1, 2, 3, 4, 5
SERVER_HELLO, MODE, BUFFER, FRAME, PARAMS, PENDING, PAUSED, RESUMED, PHOTO_DONE, ERROR = range(16, 26)
ERR_DENIED, ERR_BUSY, ERR_UNAVAILABLE, ERR_BAD_REQUEST, ERR_FAILED = 1, 3, 4, 6, 7
PAUSE_PHOTO, PAUSE_SUSPENDED = 1, 2
_MODE = struct.Struct("<5I I Q 3I 4s 2I 9f I")
_FRAME = struct.Struct("<4I Q I 4f I")
_ERROR = struct.Struct("<3I116s")
assert (_MODE.size, _FRAME.size, _ERROR.size) == (96, 48, 128)


def pick_mode(w: int, h: int, full: bool) -> dict:
    """The handheld's choice (alia-camera's pick_mode): the smallest mode covering the request,
    full field of view only if asked; else the biggest."""
    best = biggest = None
    for m in DATA["modes"]:
        if full and not m["full_fov"]:
            continue
        if biggest is None or m["w"] * m["h"] > biggest["w"] * biggest["h"]:
            biggest = m
        if m["w"] >= w and m["h"] >= h and (best is None or m["w"] * m["h"] < best["w"] * best["h"]):
            best = m
    return best or biggest


def srgb_to_linear() -> np.ndarray:
    x = np.arange(256, dtype=np.float32) / 255
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4).astype(np.float32)


LINEAR = srgb_to_linear()


class Sensor:
    """One mode's model of the sensor: per pixel, the weights that turn a linear sRGB picture into
    the raw sample there (inverse colour matrix, white balance, shading, the Bayer site)."""

    def __init__(self, mode: dict):
        self.mode = mode
        self.index = DATA["modes"].index(mode)
        self.w, self.h = mode["w"], mode["h"]
        self.stride = (self.w + 63) // 64 * 64
        p = DATA["lights"][LIGHT]["params"][self.index]
        self.params = p
        wb_r, wb_b = DATA["lights"][LIGHT]["wb"]
        self.wb = (wb_r, wb_b)
        shade = np.array(p["shading"], dtype=np.float32).reshape(12, 16, 3)

        def up(ch):  # cell centres -> every pixel (PIL's bilinear lines up pixel centres)
            return np.asarray(Image.fromarray(shade[:, :, ch], "F").resize((self.w, self.h), Image.BILINEAR))
        red, lum, blue = up(0), up(1), up(2)
        minv = np.linalg.inv(np.array(p["ccm"], dtype=np.float64).reshape(3, 3)).astype(np.float32)
        y, x = np.mgrid[0:self.h, 0:self.w]
        site = np.where(((x ^ RPOS[0]) & 1) == 0, np.where(((y ^ RPOS[1]) & 1) == 0, 0, 1),
                        np.where(((y ^ RPOS[1]) & 1) == 0, 1, 2))   # 0 R, 1 G, 2 B
        gain = np.where(site == 0, wb_r * red, np.where(site == 2, wb_b * blue, 1.0)).astype(np.float32)
        scale = (255 - BLACK) * EXPOSURE / (gain * lum)
        self.coef = np.stack([minv[site, k] * scale for k in range(3)]).astype(np.float32)   # (3, h, w)
        self.rng = np.random.default_rng(7)
        self.noise = [self.rng.standard_normal((self.h, self.w), dtype=np.float32) for _ in range(3)]

    def raw(self, linear: np.ndarray, x0=0, y0=0) -> np.ndarray:
        """Linear sRGB (h, w, 3) at (x0, y0) of this mode's picture -> raw samples (float)."""
        h, w = linear.shape[:2]
        c = self.coef[:, y0:y0 + h, x0:x0 + w]
        return BLACK + c[0] * linear[:, :, 0] + c[1] * linear[:, :, 1] + c[2] * linear[:, :, 2]

    def finish(self, raw: np.ndarray, n: int, amount: float = 1.0) -> np.ndarray:
        """Noise (shot + read, roughly the IMX219 at moderate gain), clipped to 8 bits."""
        sigma = np.sqrt(0.5 + 0.05 * np.maximum(raw - BLACK, 0)) * amount
        out = raw + self.noise[n % 3] * sigma
        np.clip(out, 0, 255, out=out)
        return out.astype(np.uint8)

    def params_bytes(self, pid: int) -> bytes:
        p = self.params
        return (struct.pack("<2I9f", PARAMS, pid, *p["ccm"]) + struct.pack("<576f", *p["shading"]) +
                struct.pack("<1024f", *p["tone"]))

    def mode_bytes(self) -> bytes:
        m = self.mode
        fx, fy, cx, cy = m["intrinsics"]
        return _MODE.pack(MODE, self.w, self.h, self.stride, 0, 0, 0, m["fps"], NBUF, 0, ORDER, BLACK,
                          int(1e9 / (m["fps"] * (self.h + 40))), fx, fy, cx, cy, 0, 0, 0, 0, 0, 0)


# ---- sources: what the camera sees

class Scene:
    """A test scene drawn once at the sensor's full resolution (3280x2464); each mode sees its own
    part of it (the handheld's crops and binning), plus a moving disc and a frame counter."""
    name = "scene"

    def __init__(self):
        self.full = self.draw()
        self.views: dict[int, np.ndarray] = {}

    @staticmethod
    def draw() -> Image.Image:
        W, H = 3280, 2464
        img = Image.new("RGB", (W, H))
        px = np.zeros((H, W, 3), dtype=np.uint8)
        yy = np.linspace(0, 1, H, dtype=np.float32)[:, None]
        px[:, :, 0] = (150 + 40 * yy).astype(np.uint8)          # a warm wall, lighter at the bottom
        px[:, :, 1] = (135 + 30 * yy).astype(np.uint8)
        px[:, :, 2] = (115 + 20 * yy).astype(np.uint8)
        img = Image.fromarray(px)
        d = ImageDraw.Draw(img)
        # the 24 patches of a colour checker (sRGB), 6 x 4
        patches = [(115, 82, 68), (194, 150, 130), (98, 122, 157), (87, 108, 67), (133, 128, 177), (103, 189, 170),
                   (214, 126, 44), (80, 91, 166), (193, 90, 99), (94, 60, 108), (157, 188, 64), (224, 163, 46),
                   (56, 61, 150), (70, 148, 73), (175, 54, 60), (231, 199, 31), (187, 86, 149), (8, 133, 161),
                   (243, 243, 242), (200, 200, 200), (160, 160, 160), (122, 122, 121), (85, 85, 85), (52, 52, 52)]
        cw, gap, x0, y0 = 300, 40, 680, 520
        d.rounded_rectangle((x0 - 60, y0 - 60, x0 + 6 * (cw + gap) + 20, y0 + 4 * (cw + gap) + 20), 40, fill=(20, 20, 22))
        for i, c in enumerate(patches):
            x, y = x0 + (i % 6) * (cw + gap), y0 + (i // 6) * (cw + gap)
            d.rounded_rectangle((x, y, x + cw, y + cw), 18, fill=c)
        # a grey ramp along the bottom, and a title
        for i in range(64):
            v = int(i * 255 / 63)
            d.rectangle((200 + i * 45, 2140, 200 + (i + 1) * 45, 2300), fill=(v, v, v))
        font = ImageFont.truetype(FONT, 120)
        d.text((W / 2, 300), "Alia Pad · simulated camera", font=font, anchor="mm", fill=(40, 36, 32))
        return img

    def view(self, s: Sensor) -> np.ndarray:
        if s.index not in self.views:
            x0, y0, w, h, b = s.mode["region"]
            part = self.full.crop((x0, y0, x0 + w, y0 + h))
            if b > 1:
                part = part.resize((w // b, h // b), Image.BOX)
            self.views[s.index] = LINEAR[np.asarray(part)]
        return self.views[s.index]

    def frame(self, s: Sensor, t: float, n: int, cache: dict) -> np.ndarray:
        base = cache.get(("base", s.index))
        if base is None:
            base = cache[("base", s.index)] = s.raw(self.view(s))
        raw = base.copy()
        lin = self.view(s)
        # the disc: across the middle, there and back every 4 s
        r = s.h // 9
        cx = int(s.w * (0.5 + 0.38 * math.sin(2 * math.pi * t / 4)))
        cy = int(s.h * 0.86)
        x0, y0, x1, y1 = max(cx - r, 0), max(cy - r, 0), min(cx + r, s.w), min(cy + r, s.h)
        patch = lin[y0:y1, x0:x1].copy()
        yy, xx = np.mgrid[y0:y1, x0:x1]
        inside = (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r
        patch[inside] = LINEAR[np.array([30, 120, 220])]
        raw[y0:y1, x0:x1] = s.raw(patch, x0, y0)
        # the frame counter, top left
        size = max(14, s.h // 24)
        label = Image.new("RGB", (size * 7, int(size * 1.5)), (20, 20, 22))
        ImageDraw.Draw(label).text((size // 3, size // 6), f"#{n:06d}", font=ImageFont.truetype(FONT, size),
                                   fill=(240, 240, 240))
        lp = LINEAR[np.asarray(label)]
        raw[8:8 + lp.shape[0], 8:8 + lp.shape[1]] = s.raw(lp, 8, 8)
        return s.finish(raw, n)

    def photo(self, s: Sensor, t: float) -> bytes:
        x0, y0, w, h, _ = s.mode["region"]
        img = self.full.crop((x0, y0, x0 + w, y0 + h))
        return jpeg(img)


class Picture:
    """A source of whole pictures (a webcam): the newest one, turned into the mode's frame."""
    name = "webcam"

    def __init__(self):
        self.latest: Image.Image | None = None
        self.lock = threading.Lock()

    def put(self, img: Image.Image):
        with self.lock:
            self.latest = img

    def get(self) -> Image.Image | None:
        with self.lock:
            return self.latest

    @staticmethod
    def fit(img: Image.Image, w: int, h: int) -> Image.Image:
        """Cut to the mode's shape (centre), scaled to its size."""
        iw, ih = img.size
        if iw * h > ih * w:
            cw = ih * w // h
            img = img.crop(((iw - cw) // 2, 0, (iw - cw) // 2 + cw, ih))
        else:
            ch = iw * h // w
            img = img.crop((0, (ih - ch) // 2, iw, (ih - ch) // 2 + ch))
        return img.resize((w, h), Image.BILINEAR)

    def frame(self, s: Sensor, t: float, n: int, cache: dict) -> np.ndarray | None:
        img = self.get()
        if img is None:
            return None
        return s.finish(s.raw(LINEAR[np.asarray(self.fit(img, s.w, s.h))]), n, 0.6)

    def photo(self, s: Sensor, t: float) -> bytes:
        img = self.get()
        return jpeg(img if img is not None else Image.new("RGB", (s.w, s.h)))

    def start(self):
        pass

    def stop(self):
        pass


class Clip(Picture):
    """The recording in camera_clip.zip, looped: which picture follows from the frame number (the
    clip's 15 fps in the mode's rate), so runs are repeatable."""
    name = "clip"
    FPS = 15

    def __init__(self):
        super().__init__()
        import zipfile
        with zipfile.ZipFile(Path(__file__).with_name("camera_clip.zip")) as z:
            self.jpegs = [z.read(n) for n in sorted(z.namelist())]
        self.last = None   # (sensor, index) -> raw: a picture shown for several frames is made once

    def frame(self, s: Sensor, t: float, n: int, cache: dict) -> np.ndarray | None:
        import io
        i = (n * self.FPS // s.mode["fps"]) % len(self.jpegs)
        if not self.last or self.last[0] != (s.index, i):
            img = Image.open(io.BytesIO(self.jpegs[i])).convert("RGB")
            self.last = ((s.index, i), s.raw(LINEAR[np.asarray(self.fit(img, s.w, s.h))]), img)
        return s.finish(self.last[1], n, 0.8)

    def photo(self, s: Sensor, t: float) -> bytes:
        return jpeg(self.last[2] if self.last else Image.open(__import__("io").BytesIO(self.jpegs[0])))


class V4l2Webcam(Picture):
    """This PC's camera on Linux (the device acart passes in): v4l2-ctl streams YUYV 640x480."""

    def __init__(self, device: str):
        super().__init__()
        self.device = device
        self.proc = None

    def start(self):
        if self.proc:
            return
        w, h = 640, 480
        cmd = ["v4l2-ctl", "-d", self.device, f"--set-fmt-video=width={w},height={h},pixelformat=YUYV",
               "--stream-mmap=4", "--stream-to=-", "--stream-count=0"]
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as e:
            log.warning("webcam %s: %s", self.device, e)
            return
        threading.Thread(target=self.read, args=(self.proc, w, h), daemon=True).start()
        log.info("webcam %s on", self.device)

    def read(self, proc, w, h):
        size = w * h * 2
        while True:
            buf = proc.stdout.read(size)
            if not buf or len(buf) < size:
                break
            yuyv = np.frombuffer(buf, dtype=np.uint8).reshape(h, w // 2, 4).astype(np.float32)
            y = np.empty((h, w), np.float32)
            y[:, 0::2], y[:, 1::2] = yuyv[:, :, 0], yuyv[:, :, 2]
            u = np.repeat(yuyv[:, :, 1] - 128, 2, axis=1)
            v = np.repeat(yuyv[:, :, 3] - 128, 2, axis=1)
            rgb = np.stack([y + 1.402 * v, y - 0.344136 * u - 0.714136 * v, y + 1.772 * u], axis=2)
            self.put(Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8)))
        log.info("webcam %s: the stream ended", self.device)

    def stop(self):
        if self.proc:
            self.proc.terminate()
            self.proc = None
            log.info("webcam %s off", self.device)


class BrowserWebcam(Picture):
    """The browser page's camera (the browser mode): web.py relays its JPEGs over FEED; we tell
    the page when to switch it on (a cart opened the camera) and off."""

    def __init__(self, server: "Server"):
        super().__init__()
        self.server = server

    def start(self):
        self.server.feed_send(b"on")

    def stop(self):
        self.server.feed_send(b"off")


def jpeg(img: Image.Image) -> bytes:
    import io
    out = io.BytesIO()
    img.save(out, "JPEG", quality=92)
    return out.getvalue()


def memfd_with(data: bytes, name: str) -> int:
    fd = os.memfd_create(name, os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    os.write(fd, data)
    os.lseek(fd, 0, os.SEEK_SET)
    return fd


# ---- the protocol server

class Server:
    def __init__(self):
        self.kind = os.environ.get("ALIA_RT_CAMERA", "clip")
        self.permission = os.environ.get("ALIA_RT_CAMERA_PERMISSION", "allow")
        self.uid = int(os.environ.get("ALIA_RT_UID", "1000"))
        self.gid = int(os.environ.get("ALIA_RT_GID", "1000"))
        self.sel = selectors.DefaultSelector()
        self.client: socket.socket | None = None
        self.feed: socket.socket | None = None
        self.source = None
        self.cache: dict = {}
        self.sensors: dict[int, Sensor] = {}
        self.reset_client()

    def reset_client(self):
        self.hello = False
        self.wants = None          # (w, h, full) asked for, once allowed
        self.granted = self.permission == "allow"
        self.pending = False
        self.suspended = False     # the supervisor froze the cart
        self.paused = False        # PAUSED sent, RESUMED owed
        self.sensor: Sensor | None = None
        self.buffers: list[tuple[int, mmap.mmap]] = []
        self.held: set[int] = set()
        self.params_id = 0
        self.n = 0
        self.next_at = None
        self.photo_done_at = None
        self.photo_jpeg = None

    # -- plumbing
    def say(self, line: str):
        print(line, flush=True)

    def send(self, data: bytes, fd: int = -1) -> bool:
        if not self.client:
            return False
        try:
            if fd >= 0:
                socket.send_fds(self.client, [data], [fd])
            else:
                self.client.send(data)
            return True
        except OSError:
            return False

    def error(self, code: int, message: str, request: int = 0, fatal: bool = True):
        self.send(_ERROR.pack(ERROR, code, request, message.encode()[:115]))
        if fatal:
            self.hang_up()

    def feed_send(self, data: bytes):
        if self.feed:
            try:
                self.feed.send(data)
            except OSError:
                pass

    # -- streams
    def make_source(self):
        if self.source is None:
            if self.kind == "scene":
                self.source = Scene()
            elif self.kind == "clip":
                self.source = Clip()
            elif self.kind == "webcam":
                self.source = (BrowserWebcam(self) if os.environ.get("ALIA_RT_BROWSER")
                               else V4l2Webcam(os.environ.get("ALIA_RT_CAMERA_DEVICE", "/dev/video0")))
        return self.source

    def free_buffers(self):
        for fd, m in self.buffers:
            m.close()
            os.close(fd)
        self.buffers, self.held = [], set()

    def start_stream(self):
        """MODE, new buffers, PARAMS (then RESUMED if paused), frames from now."""
        w, h, full = self.wants
        mode = pick_mode(w, h, full)
        i = DATA["modes"].index(mode)
        if i not in self.sensors:
            self.sensors[i] = Sensor(mode)
        s = self.sensor = self.sensors[i]
        self.free_buffers()
        self.send(s.mode_bytes())
        for b in range(NBUF):
            fd = os.memfd_create(f"alia-camera-{b}", os.MFD_CLOEXEC)
            os.ftruncate(fd, s.stride * s.h)
            self.buffers.append((fd, mmap.mmap(fd, s.stride * s.h)))
            self.send(struct.pack("<3I", BUFFER, b, s.stride * s.h), fd)
        self.params_id += 1
        self.send(s.params_bytes(self.params_id))
        if self.paused:
            self.paused = False
            self.send(struct.pack("<I", RESUMED))
        src = self.make_source()
        if hasattr(src, "start"):
            src.start()
        self.next_at = time.monotonic()
        self.say("streaming")
        log.info("streaming %dx%d @ %d (%s)", s.w, s.h, mode["fps"], self.kind)

    def stop_stream(self, reason: int | None):
        if reason is not None and not self.paused:
            self.paused = True
            self.send(struct.pack("<2I", PAUSED, reason))
        self.next_at = None
        if self.source is not None and hasattr(self.source, "stop") and reason != PAUSE_PHOTO:
            self.source.stop()

    def tick(self):
        s = self.sensor
        now = time.monotonic()
        self.next_at = max(self.next_at + 1 / s.mode["fps"], now)
        free = [i for i in range(NBUF) if i not in self.held]
        if len(self.held) >= MAX_HELD or not free:
            return
        raw = self.source.frame(s, now, self.n, self.cache)
        if raw is None:
            return
        i = free[self.n % len(free)]
        view = np.frombuffer(self.buffers[i][1], dtype=np.uint8).reshape(s.h, s.stride)
        view[:, :s.w] = raw
        wb_r, wb_b = s.wb
        exposure_us = 1000000 // s.mode["fps"]
        if self.send(_FRAME.pack(FRAME, i, self.n, self.params_id, time.monotonic_ns(), exposure_us, 2.0, wb_r, wb_b,
                                 float(LIGHT), 0)):
            self.held.add(i)
        self.n += 1

    # -- messages
    def on_message(self, data: bytes):
        if len(data) < 4:
            return self.error(ERR_BAD_REQUEST, "short message")
        kind = struct.unpack_from("<I", data)[0]
        if kind == HELLO:
            self.send(struct.pack("<2I", SERVER_HELLO, 2))
            if len(data) < 8 or struct.unpack_from("<I", data, 4)[0] != 2:
                return self.error(ERR_BAD_REQUEST, "protocol version mismatch")
            self.hello = True
        elif not self.hello:
            self.error(ERR_BAD_REQUEST, "HELLO first")
        elif kind == OPEN and len(data) >= 16:
            w, h, full = struct.unpack_from("<3I", data, 4)
            if self.kind == "none":
                return self.error(ERR_UNAVAILABLE, "no camera (acart run --camera none)")
            if self.permission == "deny":
                return self.error(ERR_DENIED, "the camera isn't allowed for this game (--camera-permission deny)")
            self.wants = (w, h, bool(full))
            if not self.granted:
                self.pending = True
                self.send(struct.pack("<I", PENDING))
                self.say("ask")
                return
            if not self.suspended:
                self.start_stream()
        elif kind == RELEASE and len(data) >= 8:
            self.held.discard(struct.unpack_from("<I", data, 4)[0])
        elif kind == PHOTO and len(data) >= 8:
            req = struct.unpack_from("<I", data, 4)[0]
            if not self.sensor or self.next_at is None:
                return self.error(ERR_BAD_REQUEST, "no stream open", req, fatal=False)
            if self.photo_done_at:
                return self.error(ERR_BUSY, "a photo is already being taken", req, fatal=False)
            # as the handheld: the preview stops for the full-resolution frame, comes back with a new
            # mode and buffers, and the JPEG arrives once developed
            self.stop_stream(PAUSE_PHOTO)
            self.photo_jpeg = (req, self.source.photo(self.sensor, time.monotonic()))
            self.photo_restart_at = time.monotonic() + 0.3
            self.photo_done_at = time.monotonic() + 1.0
        elif kind == CLOSE:
            self.hang_up()
        else:
            self.error(ERR_BAD_REQUEST, "unknown message")

    def hang_up(self):
        if self.client:
            self.sel.unregister(self.client)
            self.client.close()
            self.client = None
        if self.source is not None and hasattr(self.source, "stop"):
            self.source.stop()
        self.free_buffers()
        self.reset_client()
        self.say("off")

    def on_line(self, line: str):
        """From the supervisor."""
        if line == "suspend":
            self.suspended = True
            if self.next_at is not None:
                self.stop_stream(PAUSE_SUSPENDED)
                log.info("paused: the cart is frozen")
        elif line == "resume":
            if self.suspended:
                self.suspended = False
                if self.client and self.wants and self.granted and not self.photo_done_at:
                    self.start_stream()
        elif line.startswith("permission "):
            answer = line.split()[1]
            if not self.pending:
                return
            self.pending = False
            if answer == "granted":
                self.granted = True
                log.info("the user allowed the camera")
                if not self.suspended:
                    self.start_stream()
            else:
                self.error(ERR_DENIED, "the camera wasn't allowed")

    # -- the loop
    def run(self):
        try:
            os.unlink(SOCKET)
        except FileNotFoundError:
            pass
        lsock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        lsock.bind(SOCKET)
        os.chown(SOCKET, self.uid, self.gid)
        os.chmod(SOCKET, 0o660)
        lsock.listen(2)
        self.sel.register(lsock, selectors.EVENT_READ, "accept")
        if self.kind == "webcam" and os.environ.get("ALIA_RT_BROWSER"):
            os.makedirs(os.path.dirname(FEED), exist_ok=True)
            try:
                os.unlink(FEED)
            except FileNotFoundError:
                pass
            fsock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            fsock.bind(FEED)
            fsock.listen(1)
            self.sel.register(fsock, selectors.EVENT_READ, "feed-accept")
        os.set_blocking(sys.stdin.fileno(), False)
        self.sel.register(sys.stdin, selectors.EVENT_READ, "stdin")
        log.info("%s (%s, permission %s)", SOCKET, self.kind, self.permission)
        buf = ""
        while True:
            now = time.monotonic()
            deadlines = [t for t in (self.next_at, getattr(self, "photo_restart_at", None) if self.photo_done_at else None,
                                     self.photo_done_at) if t]
            timeout = max(0.0, min(deadlines) - now) if deadlines else None
            for key, _ in self.sel.select(timeout):
                what = key.data
                if what == "accept":
                    conn, _ = key.fileobj.accept()
                    if self.client:
                        conn.send(_ERROR.pack(ERROR, ERR_BUSY, 0, b"the camera is in use"))
                        conn.close()
                    else:
                        self.client = conn
                        self.sel.register(conn, selectors.EVENT_READ, "client")
                elif what == "client":
                    try:
                        data = self.client.recv(256)
                    except OSError:
                        data = b""
                    if not data:
                        self.hang_up()
                    else:
                        self.on_message(data)
                elif what == "feed-accept":
                    conn, _ = key.fileobj.accept()
                    if self.feed:
                        self.sel.unregister(self.feed)
                        self.feed.close()
                    self.feed = conn
                    self.sel.register(conn, selectors.EVENT_READ, "feed")
                    if self.next_at is not None:
                        self.feed_send(b"on")
                elif what == "feed":
                    try:
                        data = self.feed.recv(1 << 20)
                    except OSError:
                        data = b""
                    if not data:
                        self.sel.unregister(self.feed)
                        self.feed.close()
                        self.feed = None
                    elif isinstance(self.source, BrowserWebcam):
                        import io
                        try:
                            self.source.put(Image.open(io.BytesIO(data)).convert("RGB"))
                        except OSError:
                            pass
                elif what == "stdin":
                    chunk = sys.stdin.read()
                    if chunk == "":
                        return  # the supervisor is gone
                    buf += chunk
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        self.on_line(line.strip())
            now = time.monotonic()
            if self.photo_done_at:
                if getattr(self, "photo_restart_at", None) and now >= self.photo_restart_at:
                    self.photo_restart_at = None
                    if self.client and not self.suspended:
                        self.start_stream()
                if now >= self.photo_done_at:
                    req, data = self.photo_jpeg
                    fd = memfd_with(data, "alia-photo.jpg")
                    self.send(struct.pack("<3I", PHOTO_DONE, req, len(data)), fd)
                    os.close(fd)
                    self.photo_done_at = self.photo_jpeg = None
            if self.next_at is not None and self.sensor and now >= self.next_at:
                self.tick()


if __name__ == "__main__":
    Server().run()

"""The pause screen and the quick menu: a full-screen surface on sway's overlay layer, over
the frozen cart, drawn with Pillow into shared memory and handed to sway directly (wlkeys'
minimal Wayland client; no SDL). It takes no input: not the keyboard (the supervisor's
keyboard surface has it), not the pointer. A plain renderer: the supervisor writes what to
show as JSON lines on stdin ({"screen": null} hides it). Runs in the supervisor's own Python
environment (Pillow), which carts never see.

Why not an SDL window: opening one in the middle of a game moved the keyboard focus about,
and SDL, handed the keymap, crashed now and then (SIGSEGV / SIGBUS in SDL_PollEvent).
"""
import json
import mmap
import os
import queue
import struct
import sys
import threading

from PIL import Image, ImageDraw, ImageFont

from .wlkeys import LAYER_OVERLAY, Wayland

FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
ANCHOR_ALL = 1 | 2 | 4 | 8
DIM = 170       # alpha of the black over the frozen game
TEXT = {
    # buttons by the handheld's names; the keyboard line under them says which keys those are
    "zh": {"home": ("主页（模拟）", "游戏已挂起。按 Home 或 A 回到游戏。"),
           "paused": ("已暂停", "按任意键继续"),
           "off": ("", "熄屏（模拟）：按任意键唤醒"),
           "menu": "快捷菜单",
           "continue": "继续游戏", "language": "语言：中文（下次启动生效）",
           "screen_off": "熄屏", "quit": "退出游戏", "hint": "上下选择 · A 确定 · B 返回",
           "keyboard": "键盘：Home = {home}　A = {east}　B = {south}　方向 = {dpad}"},
    "en": {"home": ("Home (simulated)", "The game is suspended. Press Home or A to return."),
           "paused": ("Paused", "Press any button to continue"),
           "off": ("", "Screen off (simulated): press any button"),
           "menu": "Quick menu",
           "continue": "Continue", "language": "Language: English (on the next start)",
           "screen_off": "Screen off", "quit": "Quit game", "hint": "Up/Down · A select · B back",
           "keyboard": "Keyboard: Home = {home}   A = {east}   B = {south}   D-pad = {dpad}"},
}


def render(size, msg: dict | None) -> Image.Image:
    """RGBA: translucent black with the text over the frozen cart; fully clear when hidden."""
    w, h = size
    if not msg or not msg.get("screen"):
        return Image.new("RGBA", size, (0, 0, 0, 0))
    t = TEXT["zh" if msg["lang"].startswith("zh") else "en"]
    screen = msg["screen"]
    img = Image.new("RGBA", size, (0, 0, 0, 255 if screen == "off" else DIM))
    d = ImageDraw.Draw(img)
    big, small = ImageFont.truetype(FONT, h // 12), ImageFont.truetype(FONT, h // 28)
    if screen == "menu":
        d.rounded_rectangle((w * 0.3, h * 0.2, w * 0.7, h * 0.8), radius=h // 40, fill=(30, 33, 41, 255))
        d.text((w / 2, h * 0.27), t["menu"], font=ImageFont.truetype(FONT, h // 20), anchor="mm",
               fill=(230, 232, 240, 255))
        for i, item in enumerate(msg["menu"]):
            y = h * 0.38 + i * h * 0.09
            on = i == msg["index"]
            if on:
                d.rounded_rectangle((w * 0.33, y - h * 0.035, w * 0.67, y + h * 0.035), radius=h // 60,
                                    fill=(255, 196, 64, 255))
            d.text((w / 2, y), t[item], font=small, anchor="mm", fill=(20, 20, 20, 255) if on else (230, 232, 240, 255))
        d.text((w / 2, h * 0.75), t["hint"], font=small, anchor="mm", fill=(110, 116, 132, 255))
    else:
        title, line = t[screen]
        if title:
            d.text((w / 2, h * 0.45), title, font=big, anchor="mm", fill=(255, 255, 255, 255))
        d.text((w / 2, h * 0.58 if title else h * 0.5), line, font=small, anchor="mm",
               fill=(200, 200, 200, 255) if title else (90, 90, 90, 255))
    if screen != "off" and msg.get("keys"):
        d.text((w / 2, h * 0.93), t["keyboard"].format(**msg["keys"]), font=ImageFont.truetype(FONT, h // 36),
               anchor="mm", fill=(150, 150, 160, 255))
    return img


def to_argb8888(img: Image.Image) -> bytes:
    """wl_shm ARGB8888: little-endian B, G, R, A, alpha premultiplied."""
    r, g, b, a = img.convert("RGBa").split()  # "RGBa": premultiplied
    return Image.merge("RGBA", (b, g, r, a)).tobytes()


def main():
    wl = Wayland(f"{os.environ['XDG_RUNTIME_DIR']}/{os.environ['WAYLAND_DISPLAY']}")
    globals_ = {}

    def registry_event(op, p):
        if op == 0:
            name, n = struct.unpack_from("<II", p)
            iface = p[8:8 + n - 1].decode()
            version, = struct.unpack_from("<I", p, 8 + n + (-n % 4))
            globals_[iface] = (name, version)

    def bind(iface, version):
        name, have = globals_[iface]
        oid = wl.new()
        wl.send(registry, 0, struct.pack("<I", name) + wl.string(iface) + struct.pack("<II", min(version, have), oid))
        return oid

    registry = wl.new(registry_event)
    wl.send(1, 1, struct.pack("<I", registry))                    # wl_display.get_registry
    done = []
    cb = wl.new(lambda op, p: done.append(1))
    wl.send(1, 0, struct.pack("<I", cb))                          # wl_display.sync
    while not done:
        wl.dispatch()
    compositor, shm, layer_shell = bind("wl_compositor", 4), bind("wl_shm", 1), bind("zwlr_layer_shell_v1", 1)

    surface = wl.new()
    wl.send(compositor, 0, struct.pack("<I", surface))
    region = wl.new()
    wl.send(compositor, 1, struct.pack("<I", region))
    wl.send(surface, 5, struct.pack("<I", region))               # no input at all
    size = [0, 0]
    configured = threading.Event()

    def layer_event(op, p):
        if op == 0:      # configure(serial, w, h): the output's size (anchored to every edge)
            serial, w, h = struct.unpack_from("<III", p)
            wl.send(layer, 6, struct.pack("<I", serial))
            size[:] = [w, h]
            configured.set()

    layer = wl.new(layer_event)
    wl.send(layer_shell, 0, struct.pack("<III", layer, surface, 0) + struct.pack("<I", LAYER_OVERLAY)
            + wl.string("alia-overlay"))
    wl.send(layer, 1, struct.pack("<I", ANCHOR_ALL))
    wl.send(layer, 2, struct.pack("<i", -1))                      # exclusive zone: ignore others'
    wl.send(layer, 4, struct.pack("<I", 0))                       # keyboard: none
    wl.send(surface, 6)
    while not configured.is_set():
        wl.dispatch()

    w, h = size
    stride = w * 4
    fd = os.memfd_create("alia-overlay")
    os.ftruncate(fd, stride * h)
    pixels = mmap.mmap(fd, stride * h)
    pool = wl.new()
    wl.send(shm, 0, struct.pack("<II", pool, stride * h), fds=[fd])
    os.close(fd)
    buffer = wl.new()
    wl.send(pool, 0, struct.pack("<IiiiiI", buffer, 0, w, h, stride, 0))  # ARGB8888

    def draw(msg):
        pixels[:] = to_argb8888(render((w, h), msg))
        wl.send(surface, 1, struct.pack("<Iii", buffer, 0, 0))   # attach
        wl.send(surface, 2, struct.pack("<iiii", 0, 0, w, h))    # damage
        wl.send(surface, 6)                                       # commit

    draw(None)  # mapped, fully clear: shown and hidden by drawing, nothing to create later
    messages: queue.Queue = queue.Queue()

    def read():
        for line in sys.stdin:
            messages.put(json.loads(line))
        messages.put(StopIteration)

    threading.Thread(target=read, daemon=True).start()
    while True:
        wl.dispatch(timeout=0.02)
        latest = None
        while not messages.empty():
            latest = messages.get()
        if latest is StopIteration:
            return
        if latest is not None:
            draw(latest)


if __name__ == "__main__":
    main()

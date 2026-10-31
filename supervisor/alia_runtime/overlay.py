"""The pause screen and the quick menu: the cart's last frame, dimmed, with text. A plain
renderer: the supervisor writes what to show as JSON lines on stdin. Runs in the
supervisor's own Python environment (Pillow), which carts never see."""
import ctypes
import json
import sys
import threading

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
TEXT = {
    "zh": {"home": ("主页（模拟）", "游戏已挂起。按 H 或 A 回到游戏。"),
           "paused": ("已暂停", "按任意键继续"),
           "off": ("", "熄屏（模拟）：按任意键唤醒"),
           "menu": "快捷菜单",
           "continue": "继续游戏", "language": "语言：中文（下次启动生效）",
           "screen_off": "熄屏", "quit": "退出游戏", "hint": "上下选择 · A 确定 · B 返回"},
    "en": {"home": ("Home (simulated)", "The game is suspended. Press H or A to return."),
           "paused": ("Paused", "Press any button to continue"),
           "off": ("", "Screen off (simulated): press any button"),
           "menu": "Quick menu",
           "continue": "Continue", "language": "Language: English (on the next start)",
           "screen_off": "Screen off", "quit": "Quit game", "hint": "Up/Down · A select · B back"},
}


def render(frame: Image.Image, msg: dict) -> Image.Image:
    t = TEXT["zh" if msg["lang"].startswith("zh") else "en"]
    w, h = frame.size
    screen = msg["screen"]
    img = Image.new("RGB", (w, h)) if screen == "off" else ImageEnhance.Brightness(frame).enhance(0.35)
    d = ImageDraw.Draw(img)
    big, small = ImageFont.truetype(FONT, h // 12), ImageFont.truetype(FONT, h // 28)
    if screen == "menu":
        d.rounded_rectangle((w * 0.3, h * 0.2, w * 0.7, h * 0.8), radius=h // 40, fill=(30, 33, 41))
        d.text((w / 2, h * 0.27), t["menu"], font=ImageFont.truetype(FONT, h // 20), anchor="mm", fill=(230, 232, 240))
        for i, item in enumerate(msg["menu"]):
            y = h * 0.38 + i * h * 0.09
            if i == msg["index"]:
                d.rounded_rectangle((w * 0.33, y - h * 0.035, w * 0.67, y + h * 0.035), radius=h // 60, fill=(255, 196, 64))
            d.text((w / 2, y), t[item], font=small, anchor="mm", fill=(20, 20, 20) if i == msg["index"] else (230, 232, 240))
        d.text((w / 2, h * 0.75), t["hint"], font=small, anchor="mm", fill=(110, 116, 132))
    else:
        title, line = t[screen]
        if title:
            d.text((w / 2, h * 0.45), title, font=big, anchor="mm", fill=(255, 255, 255))
        d.text((w / 2, h * 0.58 if title else h * 0.5), line, font=small, anchor="mm",
               fill=(200, 200, 200) if title else (90, 90, 90))
    return img


def main():
    try:
        frame = Image.open(sys.argv[1]).convert("RGB")
    except (OSError, IndexError):
        frame = Image.new("RGB", (1280, 720))
    sdl = ctypes.CDLL("libSDL3.so.0")
    P = ctypes.c_void_p
    sdl.SDL_CreateWindow.restype = P
    sdl.SDL_CreateWindow.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_uint64]
    sdl.SDL_CreateRenderer.restype = P
    sdl.SDL_CreateRenderer.argtypes = [P, ctypes.c_char_p]
    sdl.SDL_CreateTexture.restype = P
    sdl.SDL_CreateTexture.argtypes = [P, ctypes.c_uint32, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    sdl.SDL_UpdateTexture.argtypes = [P, P, ctypes.c_char_p, ctypes.c_int]
    sdl.SDL_RenderTexture.argtypes = [P, P, P, P]
    sdl.SDL_RenderPresent.argtypes = [P]
    sdl.SDL_RenderClear.argtypes = [P]
    sdl.SDL_PollEvent.argtypes = [P]
    sdl.SDL_Init(0x20)
    win = sdl.SDL_CreateWindow(b"alia-overlay", frame.width, frame.height, 0x1)
    ren = sdl.SDL_CreateRenderer(win, None)
    tex = sdl.SDL_CreateTexture(ren, 0x17101803, 1, frame.width, frame.height)  # RGB24, streaming
    latest = {"msg": None, "dirty": False, "done": False}
    lock = threading.Lock()

    def read():
        for line in sys.stdin:
            with lock:
                latest.update(msg=json.loads(line), dirty=True)
        latest["done"] = True

    threading.Thread(target=read, daemon=True).start()
    ev = ctypes.create_string_buffer(128)
    while not latest["done"]:
        while sdl.SDL_PollEvent(ev):
            pass
        with lock:
            msg, dirty = latest["msg"], latest["dirty"]
            latest["dirty"] = False
        if dirty and msg:
            data = render(frame, msg).tobytes()
            sdl.SDL_UpdateTexture(tex, None, data, frame.width * 3)
        sdl.SDL_RenderClear(ren)
        sdl.SDL_RenderTexture(ren, tex, None, None)
        sdl.SDL_RenderPresent(ren)
        sdl.SDL_Delay(16)


if __name__ == "__main__":
    main()

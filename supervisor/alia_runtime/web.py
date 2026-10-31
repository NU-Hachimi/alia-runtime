"""The browser mode's server (docs/design.md §7): the page, and one WebSocket that carries the
screen down (JPEG pictures from capture.py, only when they change), the sound down (PCM from
the container's own PipeWire), and the keyboard and gamepad up, into the supervisor's
control socket like its own key bindings. Runs as root next to the supervisor.

  messages down (binary): 1 byte kind + payload. 1: a JPEG picture. 2: PCM, s16le stereo 48 kHz.
  messages up (JSON): {"key": "KeyW", "down": true}   a key, by its physical code
                      {"pad": {"south": true, "lx": -32767, ...}}   what changed on a gamepad
"""
import asyncio
import io
import json
import os
import socket
import subprocess
import threading
from http import HTTPStatus
from pathlib import Path

import websockets
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

from . import keys
from .capture import Capture

RUN = "/run/alia"
PORT = int(os.environ.get("ALIA_RT_PORT", "7300"))
PAGE = (Path(__file__).resolve().parent.parent / "web").resolve()
FPS = 30
# the browser's KeyboardEvent.code (a key's place) -> the SDL3 names runtime.toml [keyboard] uses
CODES = {f"Key{c}": c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"}
CODES.update({f"Digit{d}": str(d) for d in range(10)})
CODES.update({"ArrowUp": "Up", "ArrowDown": "Down", "ArrowLeft": "Left", "ArrowRight": "Right",
              "Enter": "Return", "NumpadEnter": "Keypad Enter", "Backspace": "Backspace",
              "ShiftLeft": "Left Shift", "ShiftRight": "Right Shift", "Equal": "=", "Minus": "-",
              "Space": "Space", "Escape": "Escape", "Tab": "Tab"})
ACTIONS = keys.actions()
clients: set = set()


def control(*lines: str):
    """The supervisor's control socket: the same lines its key bindings send."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect("/run/alia-rt/control")
        s.sendall("".join(line + "\n" for line in lines).encode())
    finally:
        s.close()


def on_message(text: str):
    msg = json.loads(text)
    if "key" in msg:
        name = CODES.get(msg["key"])
        word = "press" if msg.get("down") else "release"
        lines = [f"{word} {action}" for action in ACTIONS.get(name, ())]
        if lines:
            control(*lines)
    elif "pad" in msg:
        control("pad " + json.dumps(msg["pad"]))


def pictures(loop, queue: asyncio.Queue):
    """A thread: the screen, as JPEG, whenever it changed (and every second regardless, for a
    page that just connected)."""
    cap, last, since = None, None, 0.0
    period = 1 / FPS
    while True:
        start = loop.time()
        try:
            cap = cap or Capture(f"{RUN}/wayland-0")
            im = cap.grab()
        except (OSError, RuntimeError, ConnectionError):
            cap, im = None, None
        if im is not None:
            raw = im.tobytes()
            if raw != last or start - since > 1.0:
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=80)
                last, since = raw, start
                loop.call_soon_threadsafe(_offer, queue, b"\x01" + buf.getvalue())
        rest = period - (loop.time() - start)
        if rest > 0:
            threading.Event().wait(rest)


def sound(loop, queue: asyncio.Queue):
    """A thread: what the container's PipeWire plays, as raw PCM, in ~20 ms pieces."""
    while True:
        rec = subprocess.Popen(["pw-record", "--raw", "--format", "s16", "--rate", "48000", "--channels", "2",
                                "--target", "alia-browser-sink", "-P", "stream.capture.sink=true",
                                "--media-role", "Production", "-"],
                               env={"XDG_RUNTIME_DIR": RUN, "PIPEWIRE_RUNTIME_DIR": RUN},
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        while True:
            chunk = rec.stdout.read(3840)  # 960 frames: 20 ms
            if not chunk:
                break
            loop.call_soon_threadsafe(_offer, queue, b"\x02" + chunk, 25)
        rec.wait()
        threading.Event().wait(1.0)


def _offer(queue: asyncio.Queue, item: bytes, limit: int = 2):
    """Newest wins: a slow page skips pictures instead of falling behind."""
    while queue.qsize() >= limit:
        queue.get_nowait()
    queue.put_nowait(item)


async def handler(ws):
    clients.add(ws)
    try:
        async for text in ws:
            if isinstance(text, str):
                try:
                    on_message(text)
                except (ValueError, OSError):
                    pass
    except websockets.ConnectionClosed:
        pass
    finally:
        clients.discard(ws)


async def broadcast(frames: asyncio.Queue, audio: asyncio.Queue):
    """One sender for both (a connection takes one send at a time); sound goes first."""
    waiting = {}
    while True:
        for q in (frames, audio):
            if q not in waiting.values():
                waiting[asyncio.ensure_future(q.get())] = q
        done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
        for task in sorted(done, key=lambda t: waiting[t] is frames):
            del waiting[task]
            await send(task.result())


async def send(item: bytes):
    for ws in list(clients):
        try:
            await ws.send(item)
        except websockets.ConnectionClosed:
            clients.discard(ws)


def page(connection, request):
    """Plain HTTP for everything but the WebSocket: the page and its files."""
    if request.path == "/ws":
        return None
    name = "index.html" if request.path in ("/", "") else request.path.lstrip("/")
    f = (PAGE / name).resolve()
    if PAGE not in f.parents or not f.is_file():
        return Response(HTTPStatus.NOT_FOUND, "not found", Headers(), b"not found\n")
    kind = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css"}.get(f.suffix,
                                                                                                  "application/octet-stream")
    return Response(HTTPStatus.OK, "OK", Headers({"Content-Type": kind, "Cache-Control": "no-store"}), f.read_bytes())


async def main():
    loop = asyncio.get_running_loop()
    frames, audio = asyncio.Queue(), asyncio.Queue()
    threading.Thread(target=pictures, args=(loop, frames), daemon=True).start()
    if os.path.exists(f"{RUN}/pipewire-0"):
        threading.Thread(target=sound, args=(loop, audio), daemon=True).start()
    async with serve(handler, "0.0.0.0", PORT, process_request=page, max_size=2 ** 16, compression=None):
        await broadcast(frames, audio)


if __name__ == "__main__":
    asyncio.run(main())

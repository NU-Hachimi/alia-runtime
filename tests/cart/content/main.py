"""Test cart: logs what it receives (gamepad buttons, pause signals, its environment, what the
camera answers) to /save/events.txt, one line each, until SIGTERM."""
import ctypes
import os
import signal
import socket
import struct
import sys

log = open(os.path.join(os.environ["ALIA_SAVE_DIR"], "events.txt"), "a", buffering=1)
log.write(f"start lang={os.environ.get('ALIA_LANG')} cart={os.environ.get('ALIA_CART_DIR')}\n")
# the camera (protocol v2, raw: no SDK needed): HELLO, OPEN -> SERVER_HELLO, ERROR {code}
try:
    cam = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    cam.connect("/run/alia/camera.sock")
    cam.send(struct.pack("<2I", 1, 2))
    cam.send(struct.pack("<4I", 2, 1280, 720, 1))
    hello = struct.unpack_from("<2I", cam.recv(256))
    err = struct.unpack_from("<3I", cam.recv(256))
    log.write(f"camera v{hello[1]} error {err[1]}\n")
    cam.close()
except OSError as e:
    log.write(f"camera {e.strerror}\n")
signal.signal(signal.SIGUSR1, lambda *_: log.write("SIGUSR1\n"))
signal.signal(signal.SIGUSR2, lambda *_: log.write("SIGUSR2\n"))
sdl = ctypes.CDLL("libSDL3.so.0")
sdl.SDL_Init.argtypes = [ctypes.c_uint32]
sdl.SDL_CreateWindow.restype = ctypes.c_void_p
sdl.SDL_CreateWindow.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_uint64]
sdl.SDL_OpenGamepad.argtypes = [ctypes.c_uint32]
sdl.SDL_OpenGamepad.restype = ctypes.c_void_p
if not sdl.SDL_Init(0x20 | 0x2000):
    sys.exit("SDL_Init failed")
sdl.SDL_CreateWindow(b"test", 640, 360, 0)
names = {0: "south", 1: "east", 2: "west", 3: "north"}
ev = ctypes.create_string_buffer(128)
while True:
    while sdl.SDL_PollEvent(ev):
        kind = int.from_bytes(ev.raw[0:4], "little")
        if kind == 0x100:  # SDL_EVENT_QUIT: SIGTERM
            log.write("quit\n")
            sys.exit(0)
        if kind == 0x653:
            sdl.SDL_OpenGamepad(int.from_bytes(ev.raw[16:20], "little"))
            log.write("gamepad\n")
        if kind == 0x651:
            log.write(f"button {names.get(ev.raw[20], ev.raw[20])}\n")
        if kind == 0x650 and os.environ.get("ALIA_TEST_AXES") is None:  # left stick, y: -32767 up .. 0
            axis, value = ev.raw[20], int.from_bytes(ev.raw[24:26], "little", signed=True)
            if axis == 1:
                log.write(f"ly {value}\n")
    sdl.SDL_Delay(10)

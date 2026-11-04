"""Camera test cart: opens the simulated camera (protocol v2, a minimal client of its own, so it
doesn't need a given SDK), logs what arrives to /save/camera.txt, keeps frame 10 (raw), its
PARAMS and a photo in /save. A window so the supervisor sees it start; runs until SIGTERM."""
import ctypes
import json
import mmap
import os
import socket
import struct
import sys
import time

SAVE = os.environ["ALIA_SAVE_DIR"]
log = open(os.path.join(SAVE, "camera.txt"), "a", buffering=1)
sdl = ctypes.CDLL("libSDL3.so.0")
sdl.SDL_Init.argtypes = [ctypes.c_uint32]
sdl.SDL_CreateWindow.restype = ctypes.c_void_p
sdl.SDL_CreateWindow.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_uint64]
sdl.SDL_Init(0x20)
sdl.SDL_CreateWindow(b"camera test", 640, 360, 0)
ev = ctypes.create_string_buffer(128)

s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
s.connect("/run/alia/camera.sock")
s.send(struct.pack("<2I", 1, 2))
s.send(struct.pack("<4I", 2, 1280, 720, 1))
s.setblocking(False)
mode, bufs, params, frames, photo_asked = None, {}, {}, 0, False
while True:
    while sdl.SDL_PollEvent(ev):
        if int.from_bytes(ev.raw[0:4], "little") == 0x100:
            log.write(f"frames {frames}\nquit\n")
            sys.exit(0)
    try:
        data, fds, _, _ = socket.recv_fds(s, 8192, 1)
    except BlockingIOError:
        time.sleep(0.005)
        continue
    if not data:
        log.write("hung up\n")
        break
    t = struct.unpack_from("<I", data)[0]
    fd = fds[0] if fds else -1
    if t == 21:
        log.write("pending\n")
    elif t == 17:
        v = struct.unpack_from("<5I I Q 3I 4s 2I 9f I", data)
        mode = {"w": v[1], "h": v[2], "stride": v[3], "fps": v[7], "nbuf": v[8], "order": v[10].decode(),
                "black": v[11], "fx": v[13]}
        log.write(f"mode {mode['w']}x{mode['h']} {mode['order']} @ {mode['fps']}\n")
        for f in bufs.values():
            f.close()
        bufs = {}
    elif t == 18:
        i, size = struct.unpack_from("<2I", data, 4)
        bufs[i] = mmap.mmap(fd, size, prot=mmap.PROT_READ)
        os.close(fd)
        fd = -1
    elif t == 20:
        pid = struct.unpack_from("<I", data, 4)[0]
        ccm = struct.unpack_from("<9f", data, 8)
        shading = struct.unpack_from("<576f", data, 44)
        tone = struct.unpack_from("<1024f", data, 44 + 576 * 4)
        params[pid] = {"ccm": ccm, "shading": shading, "tone": tone}
    elif t == 19:
        index, seq, pid = struct.unpack_from("<3I", data, 4)
        wb = struct.unpack_from("<2f", data, 32)
        frames += 1
        if frames == 10:
            raw = bufs[index][:]
            open(os.path.join(SAVE, "frame.raw"), "wb").write(raw)
            json.dump({"mode": mode, "wb": wb, **params[pid]}, open(os.path.join(SAVE, "frame.json"), "w"))
            log.write(f"frame 10 kept (params {pid})\n")
        s.send(struct.pack("<2I", 3, index))
        if frames == 30 and not photo_asked:
            photo_asked = True
            s.send(struct.pack("<2I", 5, 7))
    elif t == 22:
        log.write(f"paused {struct.unpack_from('<I', data, 4)[0]}\n")
    elif t == 23:
        log.write("resumed\n")
    elif t == 24:
        req, size = struct.unpack_from("<2I", data, 4)
        with mmap.mmap(fd, size, prot=mmap.PROT_READ) as m:
            open(os.path.join(SAVE, "photo.jpg"), "wb").write(m[:])
        log.write(f"photo {req} {size}\n")
    elif t == 25:
        code, req = struct.unpack_from("<2I", data, 4)
        log.write(f"error {code} {req}\n")
    if fd >= 0:
        os.close(fd)

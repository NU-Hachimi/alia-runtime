"""The PC's gamepads, read with SDL3 (it knows every brand's layout) and fed to player 1 by
position: a PC pad's bottom button is south, which is B on Alia Pad. Our own virtual pad
(in /dev/input too) is skipped by name."""
import ctypes
import os
import threading
import time

from alia import runtime

# SDL_GamepadButton -> our names; SDL_GamepadAxis
SDL_BUTTONS = {0: "south", 1: "east", 2: "west", 3: "north", 4: "select", 5: "home", 6: "start",
               7: "l3", 8: "r3", 9: "l1", 10: "r1", 11: "up", 12: "down", 13: "left", 14: "right"}
SDL_AXES = {0: "lx", 1: "ly", 2: "rx", 3: "ry"}
TRIGGERS = {4: "l2", 5: "r2"}
TRIGGER_ON = 16384
POLL_S = 0.004


def start(paths: list[str], events):
    threading.Thread(target=_run, args=(paths, events), daemon=True).start()


def _run(paths, events):
    os.environ["SDL_JOYSTICK_DEVICE"] = ":".join(paths)
    os.environ["SDL_JOYSTICK_DISABLE_UDEV"] = "1"
    sdl = ctypes.CDLL("libSDL3.so.0")
    sdl.SDL_Init.argtypes = [ctypes.c_uint32]
    sdl.SDL_GetGamepads.restype = ctypes.POINTER(ctypes.c_uint32)
    sdl.SDL_GetGamepadNameForID.restype = ctypes.c_char_p
    sdl.SDL_GetGamepadNameForID.argtypes = [ctypes.c_uint32]
    sdl.SDL_OpenGamepad.restype = ctypes.c_void_p
    sdl.SDL_OpenGamepad.argtypes = [ctypes.c_uint32]
    sdl.SDL_GamepadConnected.argtypes = [ctypes.c_void_p]
    sdl.SDL_GetGamepadButton.argtypes = [ctypes.c_void_p, ctypes.c_int]
    sdl.SDL_GetGamepadAxis.restype = ctypes.c_int16
    sdl.SDL_GetGamepadAxis.argtypes = [ctypes.c_void_p, ctypes.c_int]
    if not sdl.SDL_Init(0x2000):  # SDL_INIT_GAMEPAD
        return
    ours = runtime.rules()["gamepad"]["name"].encode()
    gp, last = None, {}
    while True:
        sdl.SDL_UpdateGamepads()
        if gp is None or not sdl.SDL_GamepadConnected(gp):
            gp = None
            n = ctypes.c_int()
            ids = sdl.SDL_GetGamepads(ctypes.byref(n))
            for i in range(n.value):
                name = sdl.SDL_GetGamepadNameForID(ids[i]) or b""
                if not name.startswith(ours):
                    gp = sdl.SDL_OpenGamepad(ids[i])
                    print(f"alia-runtime: PC gamepad: {name.decode(errors='replace')} (player 1)", flush=True)
                    break
            if gp is None:
                if last:
                    events.put(("pad", {k: 0 if k[1:] in ("x", "y") else False for k in last}))
                    last = {}
                time.sleep(0.5)
                continue
        state = {name: bool(sdl.SDL_GetGamepadButton(gp, b)) for b, name in SDL_BUTTONS.items()}
        state.update({name: sdl.SDL_GetGamepadAxis(gp, a) for a, name in SDL_AXES.items()})
        state.update({name: sdl.SDL_GetGamepadAxis(gp, a) > TRIGGER_ON for a, name in TRIGGERS.items()})
        changed = {k: v for k, v in state.items() if last.get(k) != v}
        if changed:
            last.update(changed)
            events.put(("pad", changed))
        time.sleep(POLL_S)

"""alia-runtime's supervisor: what the handheld's alia-session and alia-inputd do in front
of a cart, inside the container (docs/design.md §4).

Runs as root in the container; sway, the overlay and the cart run as the PC user
(ALIA_RT_UID). Everything a cart can notice comes from alia-sdk's runtime.toml.

  environment (set by `acart run`):
    ALIA_RT_UID, ALIA_RT_GID   the PC user
    ALIA_RT_LANG               the system language (zh-CN, en)
    ALIA_RT_SIZE               the screen, 1280x720
    ALIA_RT_ARGS               the cart's extra arguments, JSON
    ALIA_RT_STATE              a file for the simulator's settings (the language), optional
    ALIA_RT_PADS               the PC's gamepads mounted in the container, colon-separated
"""
import glob
import json
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from alia import cart as cartlib
from alia import runtime

from . import keys, pads, swayipc, wlkeys
from .uinput import AXIS_MAX, AXIS_MIN, Gamepad

log = logging.getLogger("alia-runtime")
RULES = runtime.rules()
RUN = RULES["paths"]["run"]
CART = Path(RULES["paths"]["cart"])
HOLD_S = 0.5          # Home held this long: the quick menu
OVERLAY_PY = "/opt/alia-runtime/venv/bin/python"
MENU = ["continue", "language", "screen_off", "quit"]
CONTROL_DIR = "/run/alia-rt"
_ARROWS = {"Up": "↑", "Down": "↓", "Left": "←", "Right": "→"}
_KB = runtime.keyboard()
# the keys the overlay names (its keyboard line), from the platform's layout
KEY_NAMES = {b: _KB[b][0] for b in ("home", "east", "south")}
KEY_NAMES["dpad"] = "".join(_ARROWS.get(_KB[d][0], _KB[d][0]) for d in ("up", "down", "left", "right"))


class Supervisor:
    def __init__(self):
        self.uid = int(os.environ.get("ALIA_RT_UID", "1000"))
        self.gid = int(os.environ.get("ALIA_RT_GID", "1000"))
        self.lang = os.environ.get("ALIA_RT_LANG", "en")
        w, _, h = os.environ.get("ALIA_RT_SIZE", "x".join(map(str, RULES["display"]["size"]))).partition("x")
        self.size = (int(w), int(h))
        self.args = json.loads(os.environ.get("ALIA_RT_ARGS", "[]"))
        self.state_file = os.environ.get("ALIA_RT_STATE")
        self.cart = cartlib.load(CART)
        self.events: queue.Queue = queue.Queue()
        self.state = "running"     # running | home | paused | menu | off | quitting
        self.menu_index = 0
        self.home_down_at = None
        self.held: dict[str, bool] = {}
        self.sticks = {"left": set(), "right": set()}
        self.shift = False
        self.pad_state: dict = {}  # the PC gamepad, merged into player 1
        self.consumed: set = set()  # buttons the system UI took: kept from the cart until released
        self.overlay = None
        self.showing = False
        self.proc = None

    # ---- set-up
    def as_user(self) -> dict:
        groups = sorted({os.stat(p).st_gid for p in glob.glob("/dev/dri/*")})
        return dict(user=self.uid, group=self.gid, extra_groups=groups)

    def make_gamepad(self):
        """The handheld's gamepad: through /dev/uinput, or without it (Docker Desktop on Windows
        and macOS has none) the same device simulated in user space (fakepad)."""
        g = RULES["gamepad"]
        self.pad_env = {}
        try:
            self.pad = Gamepad(g["name"], g["bustype"], g["vendor"], g["product"], g["version"])
        except OSError as e:
            from .fakepad import FakeGamepad
            log.info("no /dev/uinput (%s): the gamepad is simulated in user space", e.strerror)
            self.pad = FakeGamepad(self.uid, self.gid)
            self.pad_env = self.pad.env()
            return
        sysdir = f"/sys/devices/virtual/input/{self.pad.sysname()}"
        for _ in range(50):  # the event node appears a moment after UI_DEV_CREATE
            nodes = glob.glob(sysdir + "/event*")
            if nodes:
                break
            time.sleep(0.02)
        ev = os.path.basename(nodes[0])
        major, minor = open(f"{nodes[0]}/dev").read().strip().split(":")
        os.makedirs("/dev/input", exist_ok=True)
        node = f"/dev/input/{ev}"
        if os.path.exists(node):
            os.unlink(node)
        os.mknod(node, 0o020666, os.makedev(int(major), int(minor)))

    def start_sway(self):
        os.makedirs(RUN, exist_ok=True)
        os.chown(RUN, self.uid, self.gid)
        os.chmod(RUN, 0o700)
        conf = "/tmp/alia-rt-sway.conf"
        Path(conf).write_text(keys.sway_config(*self.size))
        env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "XDG_RUNTIME_DIR": RUN,
               "WAYLAND_DISPLAY": "/run/host-wayland", "WLR_BACKENDS": "wayland",
               "WLR_NO_HARDWARE_CURSORS": "1", "XDG_CACHE_HOME": "/tmp"}
        if not os.path.exists("/run/host-wayland"):  # the browser mode, or CI: sway draws into memory
            env.update(WLR_BACKENDS="headless", WLR_LIBINPUT_NO_DEVICES="1")
            env.pop("WAYLAND_DISPLAY")
            if not glob.glob("/dev/dri/renderD*"):  # no GPU passed in (macOS, Windows, CI): software
                env["WLR_RENDERER"] = "pixman"
                os.environ["LIBGL_ALWAYS_SOFTWARE"] = "1"
        if os.environ.get("WLR_RENDERER"):
            env["WLR_RENDERER"] = os.environ["WLR_RENDERER"]
        if os.environ.get("LIBGL_ALWAYS_SOFTWARE"):
            env["LIBGL_ALWAYS_SOFTWARE"] = "1"
        argv = ["sway", "-c", conf] + (["-d"] if os.environ.get("ALIA_RT_DEBUG") else [])
        self.sway_proc = subprocess.Popen(argv, env=env, stdout=subprocess.DEVNULL,
                                          stderr=open("/tmp/sway.log", "w"), **self.as_user())
        deadline = time.time() + 10
        while time.time() < deadline:
            socks = glob.glob(f"{RUN}/sway-ipc.*.sock")
            wayland = [p for p in glob.glob(f"{RUN}/wayland-*") if not p.endswith(".lock")]
            if socks and wayland:
                self.swaysock = socks[0]
                # libwayland numbers from wayland-1; the cart sees wayland-0, as on the handheld
                if wayland[0] != f"{RUN}/wayland-0":
                    os.symlink(os.path.basename(wayland[0]), f"{RUN}/wayland-0")
                return
            if self.sway_proc.poll() is not None:
                break
            time.sleep(0.05)
        sys.exit(f"sway didn't start (exit {self.sway_proc.poll()}, {RUN}: {os.listdir(RUN)}):\n"
                 + Path("/tmp/sway.log").read_text()[-3000:])

    # ---- the browser mode (docs/design.md §7): its own PipeWire, the web server, the firewall
    def start_browser_mode(self):
        env = {"XDG_RUNTIME_DIR": RUN, "PIPEWIRE_RUNTIME_DIR": RUN, "HOME": "/tmp", "PATH": "/usr/bin:/bin",
               "DISABLE_RTKIT": "1"}
        for prog in ("pipewire", "wireplumber"):
            subprocess.Popen([prog], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **self.as_user())
        for _ in range(100):
            if os.path.exists(f"{RUN}/pipewire-0"):
                break
            time.sleep(0.05)
        time.sleep(0.5)  # WirePlumber picks the sink up as the default
        if not self.cart.permissions.get("network"):
            # the container has a network (the browser reaches the page through it); the cart,
            # like on the handheld, doesn't: nothing out for its user, loopback included
            for tool in ("iptables", "ip6tables"):
                r = subprocess.run([tool, "-A", "OUTPUT", "-m", "owner", "--uid-owner", str(self.uid), "-j", "REJECT"],
                                   capture_output=True, text=True)
                if r.returncode:
                    log.warning("%s: the cart may have network (%s)", tool, r.stderr.strip())
        self.web = subprocess.Popen([OVERLAY_PY, "-m", "alia_runtime.web"],
                                    env={"PYTHONPATH": "/opt/alia-runtime:/usr/local/lib/python3.13/dist-packages",
                                         "ALIA_RT_PORT": os.environ.get("ALIA_RT_PORT", "7300"), "HOME": "/root"})
        log.info("browser mode: the page starts on port %s", os.environ.get("ALIA_RT_HOST_PORT", "7300"))

    def start_cart(self):
        env = {"LANG": "C.UTF-8", **runtime.env(self.cart.id, self.cart.quota_kb, self.lang),
               "WAYLAND_DISPLAY": "wayland-0", "SDL_VIDEODRIVER": "wayland", **self.pad_env}
        if os.environ.get("LIBGL_ALWAYS_SOFTWARE"):
            env["LIBGL_ALWAYS_SOFTWARE"] = "1"
        if os.environ.get("ALIA_RT_NO_AUDIO") or not self.audio_works(env):
            env["SDL_AUDIO_DRIVER"] = "dummy"  # acart run --no-audio, or this PC's sound doesn't work
        content = CART / "content"
        argv = self.cart.command(self.args, root=CART)
        log.info("starting %s: %s", self.cart.id, " ".join(argv))
        self.proc = subprocess.Popen(argv, env=env, cwd=content if content.is_dir() else CART,
                                     start_new_session=True, **self.as_user())

    def audio_works(self, env: dict) -> bool:
        """The system promises a sink that never blocks an open (runtime.toml [audio]). The
        PC's PipeWire can be stuck (seen after a laptop's sleep): try an open first, in a
        child we can kill, and give the cart silence if it hangs."""
        if not os.path.exists(f"{RUN}/pipewire-0"):
            log.warning("no PipeWire on this PC: the cart runs silent")
            return False
        a = RULES["audio"]
        probe = ("import ctypes,sys\n"
                 "s=ctypes.CDLL('libSDL3.so.0')\n"
                 "s.SDL_Init.argtypes=[ctypes.c_uint32]\n"
                 "s.SDL_OpenAudioDeviceStream.restype=ctypes.c_void_p\n"
                 "s.SDL_OpenAudioDeviceStream.argtypes=[ctypes.c_uint32,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p]\n"
                 "spec=(ctypes.c_int*3)(0x8120,%d,%d)\n"
                 "sys.exit(0 if s.SDL_Init(0x10) and s.SDL_OpenAudioDeviceStream(0xFFFFFFFF,spec,None,None) else 1)\n"
                 % (a["channels"], a["rate"]))
        try:
            ok = subprocess.run(["python3", "-c", probe], env=env, timeout=3, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, **self.as_user()).returncode == 0
        except subprocess.TimeoutExpired:
            ok = False
        if not ok:
            log.warning("this PC's sound doesn't open (PipeWire stuck?): the cart runs silent")
        return ok

    # ---- the cart's process group
    def signal(self, sig):
        try:
            os.killpg(self.proc.pid, sig)
        except ProcessLookupError:
            pass

    def freeze(self):
        self.release_all()
        if self.cart.pause == "game":
            self.signal(getattr(signal, RULES["pause"]["game_suspend"]))
            time.sleep(RULES["timing"]["suspend_grace_s"])
        self.signal(signal.SIGSTOP)

    def thaw(self):
        self.signal(signal.SIGCONT)
        if self.cart.pause == "game":
            self.signal(getattr(signal, RULES["pause"]["game_resume"]))

    def quit(self):
        self.state = "quitting"
        self.show(None)
        self.signal(signal.SIGCONT)
        self.signal(signal.SIGTERM)
        threading.Timer(RULES["timing"]["term_grace_s"], lambda: self.signal(signal.SIGKILL)).start()

    # ---- the overlay (pause screen, quick menu): a surface on sway's overlay layer over the
    # frozen cart (overlay.py), running from the start; we tell it what to draw
    def start_overlay(self):
        env = {"XDG_RUNTIME_DIR": RUN, "WAYLAND_DISPLAY": "wayland-0", "HOME": "/tmp",
               "PYTHONPATH": "/opt/alia-runtime"}
        self.overlay = subprocess.Popen([OVERLAY_PY, "-m", "alia_runtime.overlay"], env=env,
                                        stdin=subprocess.PIPE, text=True, **self.as_user())

    def show(self, screen: str | None):
        if screen is None and not self.showing:
            return
        self.showing = screen is not None
        msg = {"screen": screen, "lang": self.lang, "menu": MENU, "index": self.menu_index,
               "pause": self.cart.pause, "keys": KEY_NAMES}
        try:
            self.overlay.stdin.write(json.dumps(msg) + "\n")
            self.overlay.stdin.flush()
        except (BrokenPipeError, AttributeError):
            log.warning("the overlay isn't running (exit %s)", self.overlay and self.overlay.poll())

    # ---- input
    def release_all(self):
        self.pad.set(buttons={b: 0 for b in self.pad.buttons}, axes={a: 0 for a in self.pad.axes})

    def update_pad(self):
        if self.state != "running":
            return
        for b in list(self.consumed):  # released on every device: the cart may see it again
            if not self.held.get(b) and not self.pad_state.get(b):
                self.consumed.discard(b)
        buttons = {b: (self.held.get(b, False) or self.pad_state.get(b, False)) and b not in self.consumed
                   for b in self.pad.buttons}
        tilt = AXIS_MAX // 2 if self.shift else AXIS_MAX
        axes = {}
        for side, prefix in (("left", "l"), ("right", "r")):
            d = self.sticks[side]
            x = ("right" in d) - ("left" in d)
            y = ("down" in d) - ("up" in d)
            axes[prefix + "x"] = x * tilt or self.pad_state.get(prefix + "x", 0)
            axes[prefix + "y"] = y * tilt or self.pad_state.get(prefix + "y", 0)
        self.pad.set(buttons=buttons, axes={k: max(AXIS_MIN, v) for k, v in axes.items()})

    def on_input(self, kind: str, down: bool, what: str):
        """kind: button | stick | shift; what: the button, or "left up"."""
        if kind == "shift":
            self.shift = down
        elif kind == "stick":
            side, direction = what.split()
            (self.sticks[side].add if down else self.sticks[side].discard)(direction)
        elif what == "home":
            self.on_home(down)
            return
        elif what == "power":
            if down:
                self.on_power()
            return
        else:
            self.held[what] = down
            if down and self.state != "running":
                self.on_system_button(what)
        self.update_pad()

    def on_home(self, down: bool):
        if down:
            self.home_down_at = time.time()
            # a long press opens the quick menu without waiting for release, from the game, the
            # home screen or the paused screen alike (as on the handheld)
            if self.state in ("running", "home", "paused"):
                threading.Timer(HOLD_S, self.events.put, args=(("home-held", self.home_down_at),)).start()
            return
        held = self.home_down_at and time.time() - self.home_down_at >= HOLD_S
        self.home_down_at = None
        if held:
            return
        if self.state == "running":
            self.freeze()
            self.state = "home"
            self.show("home")
        elif self.state == "paused":  # back to the home screen, the game stays frozen
            self.state = "home"
            self.show("home")
        elif self.state in ("home", "menu"):
            self.resume()

    def on_home_held(self, at):
        if self.home_down_at != at or self.state not in ("running", "home", "paused"):
            return
        if self.state == "running":
            self.freeze()
        self.state, self.menu_index = "menu", 0
        self.show("menu")

    def on_power(self):
        if self.state == "running":
            self.freeze()
        if self.state == "off":
            self.after_wake()
        elif self.state != "quitting":
            self.state = "off"
            self.show("off")

    def after_wake(self):
        """Back from home, the menu or screen off: as on the handheld, a cart with the system's
        pause screen stays frozen under "paused" until a key; one with its own resumes now."""
        if self.cart.pause == "game":
            self.resume_now()
        else:
            self.state = "paused"
            self.show("paused")

    def resume(self):
        self.after_wake()

    def resume_now(self):
        self.show(None)
        self.state = "running"
        self.thaw()
        self.update_pad()

    def on_system_button(self, button: str):
        # the system UI took this press: it doesn't reach the cart, even if the cart resumes
        # while it's still held (the A that picks "continue")
        self.consumed.add(button)
        if self.state == "paused":
            self.resume_now()
        elif self.state == "off":
            self.after_wake()
        elif self.state == "menu":
            if button in ("up", "down"):
                self.menu_index = (self.menu_index + (1 if button == "down" else -1)) % len(MENU)
                self.show("menu")
            elif button == "east":
                self.menu_action(MENU[self.menu_index])
            elif button == "south":
                self.resume()
        elif self.state == "home" and button == "east":
            self.resume()

    def menu_action(self, item: str):
        if item == "continue":
            self.resume()
        elif item == "quit":
            self.quit()
        elif item == "screen_off":
            self.state = "off"
            self.show("off")
        elif item == "language":
            self.lang = "en" if self.lang.startswith("zh") else "zh-CN"
            self.save_state()
            self.show("menu")

    def save_state(self):
        if self.state_file:
            try:
                Path(self.state_file).write_text(json.dumps({"lang": self.lang}))
            except OSError as e:
                log.warning("settings not saved: %s", e)

    # ---- threads feeding the loop
    def key_line(self, line: str):
        """ "press button east", "release stick left up": what our keyboard sends; "pad {...}":
        what changed on a gamepad (the browser mode's)."""
        if os.environ.get("ALIA_RT_DEBUG"):
            print(f"alia-runtime: {time.monotonic():10.3f} key {line.strip()}", flush=True)
        if line.startswith("pad "):
            try:
                self.events.put(("pad", json.loads(line[4:])))
            except ValueError:
                pass
            return
        parts = line.split()
        if len(parts) >= 2 and parts[0] in ("press", "release"):
            self.events.put(("key", parts[0] == "press", parts[1], " ".join(parts[2:])))

    def read_keys(self):
        """The keyboard, key by key (wlkeys: an overlay surface with the keyboard)."""
        table = keys.actions()

        def on_key(name, pressed):
            for action in table.get(name, ()):
                self.key_line(f"{'press' if pressed else 'release'} {action}")
        try:
            wlkeys.run(f"{RUN}/wayland-0", on_key)
        except (ConnectionError, OSError):
            pass  # sway is gone: we're shutting down
        except RuntimeError as e:
            log.warning("keyboard: %s", e)

    def control(self):
        """/run/alia-rt/control: the same lines as the key bindings, one per connection line,
        for tests and scripts (docker exec ... alia-rt-press east)."""
        import socket
        os.makedirs(CONTROL_DIR, exist_ok=True)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(f"{CONTROL_DIR}/control")
        srv.listen()
        while True:
            conn, _ = srv.accept()
            with conn, conn.makefile() as f:
                for line in f:
                    self.key_line(line)

    def watch_window(self):
        """The PC's window closed (the nested output is gone): quit the cart, as the user
        meant to; otherwise sway runs on with nothing to show and the run never ends."""
        if not os.path.exists("/run/host-wayland"):
            return  # headless (CI): there's no window to close
        try:
            for _ in swayipc.Sway(self.swaysock).output_events():
                if not any(o.get("active") for o in swayipc.Sway(self.swaysock).outputs()):
                    log.info("the window was closed")
                    self.events.put(("stop",))
                    return
        except ConnectionError:
            pass

    def watch_cart(self):
        code = self.proc.wait()
        self.events.put(("exit", code))

    def run(self):
        self.make_gamepad()
        self.start_sway()
        self.ipc = swayipc.Sway(self.swaysock)
        if os.environ.get("ALIA_RT_BROWSER"):
            self.start_browser_mode()
        self.start_overlay()
        self.start_cart()
        threading.Thread(target=self.read_keys, daemon=True).start()
        threading.Thread(target=self.watch_cart, daemon=True).start()
        threading.Thread(target=self.control, daemon=True).start()
        threading.Thread(target=self.watch_window, daemon=True).start()
        if os.environ.get("ALIA_RT_PADS"):
            pads.start(os.environ["ALIA_RT_PADS"].split(":"), self.events)
        signal.signal(signal.SIGTERM, lambda *_: self.events.put(("stop",)))
        signal.signal(signal.SIGINT, lambda *_: self.events.put(("stop",)))
        while True:
            ev = self.events.get()
            if ev[0] == "key":
                _, down, kind, what = ev
                self.on_input(kind, down, what if kind != "button" else what)
            elif ev[0] == "pad":       # the PC gamepad: buttons and axes, and its Home
                _, state = ev
                home = state.pop("home", None)
                if home is not None:
                    self.on_home(home)
                for b, v in state.items():
                    if b in self.pad.buttons and v and not self.pad_state.get(b) and self.state != "running":
                        self.on_system_button(b)
                self.pad_state.update(state)
                self.update_pad()
            elif ev[0] == "home-held":
                self.on_home_held(ev[1])
            elif ev[0] == "stop" and self.state != "quitting":
                self.quit()
            elif ev[0] == "exit":
                code = ev[1]
                log.info("%s exited: %s", self.cart.id, code)
                self.show(None)
                if self.overlay:
                    self.overlay.terminate()
                self.sway_proc.terminate()
                self.pad.close()
                return code if code >= 0 else 128 - code


def main():
    logging.basicConfig(level=logging.INFO, format="alia-runtime: %(message)s")
    try:
        sys.exit(Supervisor().run())
    except cartlib.CartError as e:
        sys.exit(f"alia-runtime: {e}")


if __name__ == "__main__":
    main()

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

from . import keys, pads, swayipc
from .uinput import AXIS_MAX, AXIS_MIN, Gamepad

log = logging.getLogger("alia-runtime")
RULES = runtime.rules()
RUN = RULES["paths"]["run"]
CART = Path(RULES["paths"]["cart"])
HOLD_S = 0.5          # Home held this long: the quick menu
OVERLAY_PY = "/opt/alia-runtime/venv/bin/python"
MENU = ["continue", "language", "screen_off", "quit"]
CONTROL_DIR = "/run/alia-rt"


class Supervisor:
    def __init__(self):
        self.uid = int(os.environ.get("ALIA_RT_UID", "1000"))
        self.gid = int(os.environ.get("ALIA_RT_GID", "1000"))
        self.lang = os.environ.get("ALIA_RT_LANG", "en")
        w, _, h = os.environ.get("ALIA_RT_SIZE", "1280x720").partition("x")
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
        self.overlay = None
        self.proc = None

    # ---- set-up
    def as_user(self) -> dict:
        groups = sorted({os.stat(p).st_gid for p in glob.glob("/dev/dri/*")})
        return dict(user=self.uid, group=self.gid, extra_groups=groups)

    def make_gamepad(self):
        g = RULES["gamepad"]
        self.pad = Gamepad(g["name"], g["bustype"], g["vendor"], g["product"], g["version"])
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
        if not os.path.exists("/run/host-wayland"):  # no screen (CI): sway draws into memory
            env.update(WLR_BACKENDS="headless", WLR_LIBINPUT_NO_DEVICES="1", WLR_RENDERER="pixman")
            env.pop("WAYLAND_DISPLAY")
            os.environ["LIBGL_ALWAYS_SOFTWARE"] = "1"
        if os.environ.get("WLR_RENDERER"):
            env["WLR_RENDERER"] = os.environ["WLR_RENDERER"]
        if os.environ.get("LIBGL_ALWAYS_SOFTWARE"):
            env["LIBGL_ALWAYS_SOFTWARE"] = "1"
        self.sway_proc = subprocess.Popen(["sway", "-c", conf], env=env, stdout=subprocess.DEVNULL,
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

    def start_cart(self):
        env = {"LANG": "C.UTF-8", **runtime.env(self.cart.id, self.cart.quota_kb, self.lang),
               "WAYLAND_DISPLAY": "wayland-0", "SDL_VIDEODRIVER": "wayland"}
        if os.environ.get("LIBGL_ALWAYS_SOFTWARE"):
            env["LIBGL_ALWAYS_SOFTWARE"] = "1"
        if os.environ.get("ALIA_RT_NO_AUDIO") or not os.path.exists(f"{RUN}/pipewire-0"):
            env["SDL_AUDIO_DRIVER"] = "dummy"  # acart run --no-audio, or no PipeWire on this PC
        content = CART / "content"
        argv = self.cart.command(self.args, root=CART)
        log.info("starting %s: %s", self.cart.id, " ".join(argv))
        self.proc = subprocess.Popen(argv, env=env, cwd=content if content.is_dir() else CART,
                                     start_new_session=True, **self.as_user())

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

    # ---- the overlay (pause screen, quick menu): a renderer we tell what to draw
    def show(self, screen: str | None):
        if screen is None:
            if self.overlay:
                self.ipc.command("workspace 1")
                self.overlay.stdin.close()
                self.overlay = None
            return
        if self.overlay is None:
            shot = "/tmp/alia-rt-frame.ppm"
            subprocess.run(["grim", "-t", "ppm", shot], env={"XDG_RUNTIME_DIR": RUN, "WAYLAND_DISPLAY": "wayland-0"},
                           **self.as_user(), check=False)
            self.ipc.command("workspace 2")
            env = {"XDG_RUNTIME_DIR": RUN, "WAYLAND_DISPLAY": "wayland-0", "SDL_VIDEODRIVER": "wayland",
                   "SDL_APP_ID": "alia-overlay", "HOME": "/tmp", "PYTHONPATH": "/opt/alia-runtime"}
            self.overlay = subprocess.Popen([OVERLAY_PY, "-m", "alia_runtime.overlay", shot], env=env,
                                            stdin=subprocess.PIPE, text=True, **self.as_user())
        msg = {"screen": screen, "lang": self.lang, "menu": MENU, "index": self.menu_index,
               "pause": self.cart.pause}
        self.overlay.stdin.write(json.dumps(msg) + "\n")
        self.overlay.stdin.flush()

    # ---- input
    def release_all(self):
        self.pad.set(buttons={b: 0 for b in self.pad.buttons}, axes={a: 0 for a in self.pad.axes})

    def update_pad(self):
        if self.state != "running":
            return
        buttons = {b: self.held.get(b, False) or self.pad_state.get(b, False) for b in self.pad.buttons}
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
            if self.state == "running":  # a long press opens the menu without waiting for release
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
        elif self.state in ("home", "menu"):
            self.resume()

    def on_home_held(self, at):
        if self.home_down_at == at and self.state == "running":
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
        if self.state == "paused":
            self.held[button] = False  # the key that wakes it doesn't reach the game
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
        """ "press button east", "release stick left up": what our key bindings send."""
        parts = line.split()
        if len(parts) >= 2 and parts[0] in ("press", "release"):
            self.events.put(("key", parts[0] == "press", parts[1], " ".join(parts[2:])))

    def read_keys(self):
        try:
            for command in swayipc.Sway(self.swaysock).bindings():
                if command.startswith("nop alia "):
                    self.key_line(command[len("nop alia "):])
        except ConnectionError:
            pass  # sway is gone: we're shutting down

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

    def watch_cart(self):
        code = self.proc.wait()
        self.events.put(("exit", code))

    def run(self):
        self.make_gamepad()
        self.start_sway()
        self.ipc = swayipc.Sway(self.swaysock)
        self.start_cart()
        threading.Thread(target=self.read_keys, daemon=True).start()
        threading.Thread(target=self.watch_cart, daemon=True).start()
        threading.Thread(target=self.control, daemon=True).start()
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

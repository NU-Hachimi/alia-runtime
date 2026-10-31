"""The platform's PC keyboard layout (alia-sdk runtime.toml [keyboard], SDL3 key names) as
sway key bindings. Every bound key is the supervisor's: sway reports it on its IPC
(binding events) and the cart never sees the keyboard, as on the handheld."""
from alia import runtime

# SDL3 key name -> xkb keysym, where they differ (letters are lowercase in xkb)
XKB = {"Return": "Return", "Keypad Enter": "KP_Enter", "=": "equal", "-": "minus",
       "Backspace": "BackSpace", "Up": "Up", "Down": "Down", "Left": "Left", "Right": "Right",
       "Left Shift": "Shift_L", "Right Shift": "Shift_R"}


def keysym(name: str) -> str:
    return XKB.get(name, name.lower() if len(name) == 1 else name)


def bindings() -> list[tuple[str, str]]:
    """(keysym, action): actions are "button NAME", "stick left|right DIR", "shift"."""
    kb = runtime.keyboard()
    out = []
    for button, keys in kb.items():
        if button == "half_tilt":
            out += [(keysym(k), "shift") for k in keys]
        elif isinstance(keys, list):
            out += [(keysym(k), f"button {button}") for k in keys]
    for stick in ("left_stick", "right_stick"):
        for direction, k in kb[stick].items():
            out.append((keysym(k), f"stick {stick.split('_')[0]} {direction}"))
    return out


def sway_config(width: int, height: int) -> str:
    """The nested compositor's configuration: one fullscreen app, the keyboard bound."""
    lines = ["xwayland disable", f"output * resolution {width}x{height} bg #000000 solid_color",
             "default_border none", "focus_follows_mouse no", "seat * hide_cursor 2000",
             'for_window [app_id=".*"] fullscreen enable', 'for_window [title=".*"] fullscreen enable',
             # the overlay (pause screen, quick menu) goes over the frozen cart
             'for_window [app_id="alia-overlay"] fullscreen enable, focus']
    for key, action in bindings():
        if action == "shift":
            lines += [f"bindsym --no-repeat {key} nop alia press {action}",
                      f"bindsym --release Shift+{key} nop alia release {action}",
                      f"bindsym --release {key} nop alia release {action}"]
            continue
        for mod in ("", "Shift+"):  # held Shift (half tilt) must not hide the other keys
            lines += [f"bindsym --no-repeat {mod}{key} nop alia press {action}",
                      f"bindsym --release {mod}{key} nop alia release {action}"]
    return "\n".join(lines) + "\n"

"""The platform's PC keyboard layout (alia-sdk runtime.toml [keyboard], SDL3 key names) as
the supervisor's keyboard reader (wlkeys) looks them up. Every key goes to the supervisor;
the cart never sees the keyboard, as on the handheld."""
from alia import runtime



def actions() -> dict[str, list[str]]:
    """SDL3 key name -> actions: "button NAME", "stick left|right DIR", "shift"."""
    kb = runtime.keyboard()
    out: dict[str, list[str]] = {}

    def add(name, action):
        out.setdefault(name, []).append(action)

    for button, keys in kb.items():
        if button == "half_tilt":
            for k in keys:
                add(k, "shift")
        elif isinstance(keys, list):
            for k in keys:
                add(k, f"button {button}")
    for stick in ("left_stick", "right_stick"):
        for direction, k in kb[stick].items():
            add(k, f"stick {stick.split('_')[0]} {direction}")
    return out


def sway_config(width: int, height: int) -> str:
    """The nested compositor's configuration: one fullscreen app. No key bindings: the keyboard
    goes to the supervisor's overlay surface (wlkeys), key by key."""
    lines = ["xwayland disable", f"output * resolution {width}x{height} bg #000000 solid_color",
             "default_border none", "focus_follows_mouse no", "seat * hide_cursor 2000",
             'for_window [app_id=".*"] fullscreen enable', 'for_window [title=".*"] fullscreen enable']
    return "\n".join(lines) + "\n"

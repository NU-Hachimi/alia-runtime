#!/bin/bash
# The simulated camera end to end (headless): the "ask" prompt answered, modes, buffers, PARAMS,
# frames, a photo, a suspend and resume; then frame 10 developed on the PC (as the reference
# shader would) must give back the scene's grey patches as grey.
#   tests/camera.sh [IMAGE]
set -euo pipefail
cd "$(dirname "$0")"
IMAGE=${1:-alia-runtime:dev}
docker rm -f alia-rt-cam > /dev/null 2>&1 || true
SAVE=$(mktemp -d) && chmod 777 "$SAVE"
docker run -d --name alia-rt-cam --rm --init -v "$PWD/camera-cart:/cart:ro" -v "$SAVE:/save" \
    -e ALIA_RT_UID="$(id -u)" -e ALIA_RT_GID="$(id -g)" -e ALIA_RT_CAMERA=scene -e ALIA_RT_CAMERA_PERMISSION=ask \
    --network none "$IMAGE" > /dev/null
trap 'docker rm -f alia-rt-cam > /dev/null 2>&1 || true' EXIT
ctl() { docker exec alia-rt-cam python3 -m alia_runtime.control "$@"; }
for _ in $(seq 100); do grep -q pending "$SAVE/camera.txt" 2> /dev/null && break; sleep 0.1; done
sleep 0.5
ctl tap button east; sleep 3          # allow; frames; the photo at frame 30
ctl tap button home; sleep 1          # the home screen: the camera pauses
ctl tap button home; sleep 0.5        # back to the game's "paused" screen...
ctl tap button east; sleep 1.5        # ...any button: it runs, the camera with a new mode, resumed
ctl hold button home 0.8; sleep 0.5   # the quick menu: quit
for _ in 1 2 3; do ctl tap button down; done
ctl tap button east
for _ in $(seq 50); do docker ps -q --filter name=alia-rt-cam | grep -q . || break; sleep 0.2; done
echo "cart saw:"; sed 's/^/  /' "$SAVE/camera.txt"
want_lines=("pending" "mode 1640x1232 BGGR @ 30" "frame 10 kept" "paused 1" "resumed" "photo 7" "paused 2")
for w in "${want_lines[@]}"; do grep -q "^$w" "$SAVE/camera.txt" || { echo "FAIL: no '$w'"; exit 1; }; done
[ "$(grep -c '^resumed' "$SAVE/camera.txt")" -ge 2 ] || { echo "FAIL: resumed after the photo and after home"; exit 1; }
frames=$(sed -n 's/^frames //p' "$SAVE/camera.txt")
[ "${frames:-0}" -ge 60 ] || { echo "FAIL: only ${frames:-0} frames"; exit 1; }
python3 - "$SAVE" <<'PY'
import json, sys, struct
d = sys.argv[1]
j = json.load(open(f"{d}/frame.json")); m = j["mode"]
raw = open(f"{d}/frame.raw", "rb").read()
w, h, st, b = m["w"], m["h"], m["stride"], m["black"] / 255
def px(x, y): return raw[y * st + x] / 255
# develop a 2x2-cell average over the light grey patch (the scene's patch 19: x 680+0*340.., y 520+3*340 at
# full resolution -> binned by 2) as the reference shader's steps: black, WB x colour shading, lum, ccm
def develop(cx, cy, n=8):
    acc = [0, 0, 0]
    for y in range(cy - n, cy + n, 2):
        for x in range(cx - n, cx + n, 2):
            bl, g1, g2, r = px(x, y), px(x + 1, y), px(x, y + 1), px(x + 1, y + 1)  # BGGR
            acc[0] += r; acc[1] += (g1 + g2) / 2; acc[2] += bl
    k = (n * n)
    rgb = [max(a / k - b, 0) / (1 - b) for a in acc]
    sx, sy = min(15, cx * 16 // w), min(11, cy * 12 // h)
    sh = j["shading"][(sy * 16 + sx) * 3:(sy * 16 + sx) * 3 + 3]
    rgb = [rgb[0] * j["wb"][0] * sh[0], rgb[1], rgb[2] * j["wb"][1] * sh[2]]
    rgb = [c * sh[1] for c in rgb]
    c = j["ccm"]
    return [c[i * 3] * rgb[0] + c[i * 3 + 1] * rgb[1] + c[i * 3 + 2] * rgb[2] for i in range(3)]
for name, (fx, fy) in {"white": (680 + 150, 520 + 3 * 340 + 150), "mid grey": (680 + 3 * 340 + 150, 520 + 3 * 340 + 150)}.items():
    r, g, bb = develop(fx // 2 & ~1, fy // 2 & ~1)   # cells start on even (B) sites
    print(f"{name}: linear R/G {r / g:.3f}  B/G {bb / g:.3f}")
    assert abs(r / g - 1) < 0.06 and abs(bb / g - 1) < 0.06, f"{name} isn't grey"
print("greys develop as grey")
PY
file "$SAVE/photo.jpg" | grep -q JPEG || { echo "FAIL: the photo isn't a JPEG"; exit 1; }
echo PASS

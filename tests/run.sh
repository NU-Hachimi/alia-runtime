#!/bin/bash
# End-to-end test of the image, headless (CI): run the test cart, press buttons, suspend and
# resume it, quit it from the quick menu; check what the cart saw.
#   tests/run.sh [IMAGE]
set -euo pipefail
cd "$(dirname "$0")"
IMAGE=${1:-alia-runtime:dev}
SAVE=$(mktemp -d) && chmod 777 "$SAVE"
docker run -d --name alia-rt-test --rm --init --device /dev/uinput --device-cgroup-rule 'c 13:* rmw' \
    -v "$PWD/cart:/cart:ro" -v "$SAVE:/save" -e ALIA_RT_UID="$(id -u)" -e ALIA_RT_GID="$(id -g)" \
    -e ALIA_RT_LANG=zh-CN --network none "$IMAGE" > /dev/null
trap 'docker rm -f alia-rt-test > /dev/null 2>&1 || true' EXIT
ctl() { docker exec alia-rt-test python3 -m alia_runtime.control "$@"; }
for _ in $(seq 50); do grep -q gamepad "$SAVE/events.txt" 2> /dev/null && break; sleep 0.2; done
# the keyboard as the user's reaches sway (a virtual keyboard: press, hold, release W = stick up, back)
kbd() { docker exec -u "$(id -u)" -e XDG_RUNTIME_DIR=/run/alia -e WAYLAND_DISPLAY=wayland-0 alia-rt-test wtype "$@"; }
kbd -P w; sleep 0.4; kbd -p w; sleep 0.4
ctl tap button east; ctl tap button north; sleep 0.3
ctl tap button home; sleep 1           # suspend: SIGUSR1, frozen
ctl tap button south; sleep 0.3        # frozen: must not reach the cart
ctl tap button home; sleep 1           # resume: SIGUSR2 (pause = "game": no paused screen)
ctl tap button west; sleep 0.3
ctl hold button home 0.8; sleep 0.5    # quick menu
for _ in 1 2 3; do ctl tap button down; done
ctl tap button east                    # quit
for _ in $(seq 50); do docker ps -q --filter name=alia-rt-test | grep -q . || break; sleep 0.2; done
sticks=$(grep '^ly' "$SAVE/events.txt" | grep -v '^ly 0$' -c || true)
released=$(grep '^ly' "$SAVE/events.txt" | tail -1)
echo "stick: $sticks deflections, last: $released"
[ "$sticks" -ge 1 ] && [ "$released" = "ly 0" ] || { echo "FAIL: W held then released should leave the stick at 0"; exit 1; }
got=$(grep -v '^ly' "$SAVE/events.txt" | tr '\n' ' ')
want="start lang=zh-CN cart=/cart gamepad button east button north SIGUSR1 SIGUSR2 button west SIGUSR1 quit "   # quitting a frozen cart: SIGCONT, SIGTERM (no SIGUSR2), as alia-session does
echo "cart saw: $got"
[ "$got" = "$want" ] || { echo "FAIL: expected: $want"; exit 1; }
echo PASS

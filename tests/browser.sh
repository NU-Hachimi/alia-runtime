#!/bin/bash
# The browser mode, headless (CI): the page is served; pictures and sound come down the
# WebSocket; the keyboard (by key position) and a gamepad go up and reach the cart; the
# cart has no network although the container has.
#   tests/browser.sh [IMAGE]
set -euo pipefail
cd "$(dirname "$0")"
IMAGE=${1:-alia-runtime:dev}
# NO_UINPUT=1: as on Docker Desktop (Windows, macOS): no /dev/uinput, the gamepad simulated in user space
UINPUT=(--device /dev/uinput --device-cgroup-rule 'c 13:* rmw')
[ -n "${NO_UINPUT:-}" ] && UINPUT=()
docker rm -f alia-rt-browser > /dev/null 2>&1 || true   # a run before this one still going away
SAVE=$(mktemp -d) && chmod 777 "$SAVE"
docker run -d --name alia-rt-browser --rm --init "${UINPUT[@]}" \
    --cap-add NET_ADMIN -p 127.0.0.1:7399:7300 -v "$PWD/cart:/cart:ro" -v "$SAVE:/save" \
    -e ALIA_RT_UID="$(id -u)" -e ALIA_RT_GID="$(id -g)" -e ALIA_RT_BROWSER=1 "$IMAGE" > /dev/null
trap 'docker rm -f alia-rt-browser > /dev/null 2>&1 || true' EXIT
for _ in $(seq 50); do grep -q gamepad "$SAVE/events.txt" 2> /dev/null && break; sleep 0.2; done
code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:7399/)
[ "$code" = 200 ] || { echo "FAIL: the page: HTTP $code"; exit 1; }
got=$(docker exec alia-rt-browser /opt/alia-runtime/venv/bin/python -c '
import asyncio, json, websockets
async def main():
    async with websockets.connect("ws://127.0.0.1:7300/ws") as ws:
        for msg in ({"key": "KeyX", "down": True}, {"key": "KeyX", "down": False},
                    {"pad": {"north": True}}, {"pad": {"north": False}}):
            await ws.send(json.dumps(msg)); await asyncio.sleep(0.15)
        kinds = set(); end = asyncio.get_running_loop().time() + 3
        while asyncio.get_running_loop().time() < end and len(kinds) < 2:
            kinds.add((await ws.recv())[0])
        print(" ".join(sorted({1: "pictures", 2: "sound"}[k] for k in kinds)))
asyncio.run(main())')
echo "down the WebSocket: $got"
[ "$got" = "pictures sound" ] || { echo "FAIL: expected pictures and sound"; exit 1; }
sleep 0.3
saw=$(grep '^button' "$SAVE/events.txt" | tr '\n' ' ')
echo "cart saw: $saw"
[ "$saw" = "button east button north " ] || { echo "FAIL: expected the keyboard's X (A, east) and the pad's north"; exit 1; }
net=$(docker exec -u "$(id -u)" alia-rt-browser python3 -c '
import socket
try: socket.create_connection(("1.1.1.1", 53), 2); print("open")
except OSError: print("closed")')
echo "the cart's network: $net"
[ "$net" = closed ] || { echo "FAIL: the cart reached the network"; exit 1; }
echo PASS

"""Send key lines to a running supervisor (tests, scripts):
  python3 -m alia_runtime.control press button east   |   tap button east   |   hold button home 0.8"""
import socket
import sys
import time


def send(*lines: str):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect("/run/alia-rt/control")
    s.sendall("".join(line + "\n" for line in lines).encode())
    s.close()


def main(argv):
    if argv[0] in ("tap", "hold"):
        held = float(argv[3]) if argv[0] == "hold" and len(argv) > 3 else 0.1
        send(f"press {argv[1]} {argv[2]}")
        time.sleep(held)
        send(f"release {argv[1]} {argv[2]}")
    else:
        send(" ".join(argv))


if __name__ == "__main__":
    main(sys.argv[1:])

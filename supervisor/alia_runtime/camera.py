"""The camera, as far as the simulator has one yet: none. A cart whose manifest declares
[permissions] camera finds the camera socket where the handheld puts it (/run/alia/camera.sock)
and gets the answer a handheld without a camera gives: protocol v2 (the devkit's
include/alia/camera_proto.h), SERVER_HELLO, then ERROR UNAVAILABLE on OPEN. So a cart's
"no camera" path runs here as it would on the device. A cart without the permission finds no
socket, as on the handheld.

Later (docs/design.md §11): a camera for real, replaying recorded IMX219 clips or turning this
PC's webcam back into the same Bayer frames.
"""
from __future__ import annotations

import logging
import os
import socket
import struct
import threading

log = logging.getLogger("alia-runtime")

VERSION = 2
HELLO, OPEN, CLOSE, PHOTO = 1, 2, 4, 5
SERVER_HELLO, ERROR = 16, 25
ERR_UNAVAILABLE, ERR_BAD_REQUEST = 4, 6
_ERROR = struct.Struct("<3I116s")   # struct cam_error: type, code, request, message


def _error(code: int, request: int, message: str) -> bytes:
    return _ERROR.pack(ERROR, code, request, message.encode()[:115])


def _serve(conn: socket.socket):
    try:
        while True:
            msg = conn.recv(256)
            if len(msg) < 4:
                return
            kind = struct.unpack_from("<I", msg)[0]
            if kind == HELLO:
                conn.send(struct.pack("<2I", SERVER_HELLO, VERSION))
            elif kind == OPEN:
                conn.send(_error(ERR_UNAVAILABLE, 0, "the simulator has no camera yet"))
                return
            elif kind == PHOTO and len(msg) >= 8:
                conn.send(_error(ERR_BAD_REQUEST, struct.unpack_from("<I", msg, 4)[0], "no stream open"))
            elif kind == CLOSE:
                return
    except OSError:
        pass
    finally:
        conn.close()


def serve(path: str, uid: int, gid: int):
    """Listen at path (owned by the cart's user) in a daemon thread."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    s.bind(path)
    os.chown(path, uid, gid)
    os.chmod(path, 0o660)
    s.listen(2)

    def loop():
        while True:
            conn, _ = s.accept()
            threading.Thread(target=_serve, args=(conn,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    log.info("camera: %s (none in the simulator yet: OPEN answers UNAVAILABLE)", path)

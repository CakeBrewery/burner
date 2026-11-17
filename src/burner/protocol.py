"""Wire format between a session host and its clients.

Each session host listens on a Unix socket. A client opens one connection
per call, sends one request, and reads one reply. An attach connection
stays open and carries data both ways.

Every message is a frame: one type byte, a 4-byte big-endian payload length,
then the payload.

- ``J`` frames hold one UTF-8 JSON object. Requests carry ``v`` (the
  sender's protocol version) and ``op``. Replies carry ``v``, ``ok``, and
  either result fields or ``error``. Events sent during attach carry
  ``event`` instead of ``ok``.
- ``D`` frames hold raw terminal bytes, used only on an attach connection.

Requests: ``info``, ``screen`` (``lines``), ``write`` (``data``, base64),
``stop`` (``grace``), ``attach`` (``cols``, ``rows``). On an attach
connection the client may also send ``resize`` (``cols``, ``rows``) and
``detach``. The host sends the events ``exit`` (``code`` or ``signal``) and
``detached`` (``reason``).

Version rule: a host keeps running the code it started with, so a client may
be newer or older than the host it talks to. The frame format never changes.
New ops and fields are added, never changed in meaning. A host answers an op
it does not know with ``ok: false`` and keeps the connection open, and both
sides ignore fields they do not know. ``VERSION`` goes up when an op is
added, so a client can tell from a reply what the host supports.

The start request is not sent over the socket. The starter writes one ``J``
frame (argv, env, cwd, and options) to the host's stdin pipe, and the host
answers with one JSON line on its stdout. This keeps argv and the environment
off every command line and off the disk.
"""

from __future__ import annotations

import json
import socket
import struct
from typing import Any

VERSION = 1
JSON = b"J"
DATA = b"D"
HEADER = struct.Struct(">cI")
MAX_FRAME = 1 << 20  # Requests and attach input.
MAX_REPLY = 16 << 20  # Replies hold a whole screen, which may be large.
MAX_START_FRAME = 64 << 20


class ProtocolError(Exception):
    """A peer sent bytes that do not form a valid frame."""


def frame(kind: bytes, payload: bytes) -> bytes:
    return HEADER.pack(kind, len(payload)) + payload


def message(obj: dict[str, Any]) -> bytes:
    return frame(JSON, json.dumps({"v": VERSION, **obj}, ensure_ascii=False).encode())


def append_text_reply(buf: bytearray, rows: list[str]) -> None:
    """Append the frame of message({"ok": True, "text": "\\n".join(rows)}) to buf.

    The JSON is encoded a row at a time into buf, so a large screen is held
    once as rows and once as bytes, not as several full copies of its text.
    """
    start = len(buf)
    buf += HEADER.pack(JSON, 0)
    buf += f'{{"v": {VERSION}, "ok": true, "text": "'.encode()
    for i, row in enumerate(rows):
        if i:
            buf += b"\\n"
        buf += json.dumps(row, ensure_ascii=False)[1:-1].encode()
    buf += b'"}'
    HEADER.pack_into(buf, start, JSON, len(buf) - start - HEADER.size)


def parse(buf: bytearray, limit: int = MAX_FRAME) -> tuple[bytes, Any] | None:
    """Remove one complete frame from the front of buf.

    Returns ``(kind, value)`` with a dict for ``J`` and bytes for ``D``, or
    None when buf does not yet hold a whole frame.
    """
    if len(buf) < HEADER.size:
        return None
    kind, size = HEADER.unpack_from(buf)
    if kind not in (JSON, DATA):
        raise ProtocolError("unknown frame type")
    if size > limit:
        raise ProtocolError("frame too large")
    end = HEADER.size + size
    if len(buf) < end:
        return None
    payload = bytes(buf[HEADER.size:end])
    del buf[:end]
    if kind == DATA:
        return kind, payload
    try:
        value = json.loads(payload)
    except ValueError as exc:
        raise ProtocolError("frame is not valid JSON") from exc
    except RecursionError:
        raise ProtocolError("frame nests too deeply") from None
    if not isinstance(value, dict):
        raise ProtocolError("frame is not a JSON object")
    return kind, value


def read_frame(
    sock: socket.socket, buf: bytearray, limit: int = MAX_REPLY
) -> tuple[bytes, Any]:
    """Block until one frame arrives on sock. Raises EOFError on close."""
    while True:
        got = parse(buf, limit)
        if got is not None:
            return got
        chunk = sock.recv(65536)
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk

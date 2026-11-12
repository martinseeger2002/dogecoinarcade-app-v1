"""Arcade LoRa frames: the wire format in PROTOCOL.md.

A frame is 5 header bytes and a body. Bodies longer than one frame are split
into up to 16 parts that share a type and an id; `Joiner` puts them back
together. The field helpers (`Writer`, `Reader`) are the only way bodies are
built or read, so the C side has one shape to match.
"""
from __future__ import annotations

import struct
import time
from dataclasses import dataclass

MAGIC = 0xA7
HEADER = 5
MAX_FRAME = 200    # Meshtastic: 255 - 16 header - 12 PKC = 227 B of encoded Data, less the protobuf wrapper
MAX_BODY = MAX_FRAME - HEADER
MAX_PARTS = 16
JOIN_TIMEOUT = 120.0
PORTNUM = 256                      # Meshtastic PRIVATE_APP

# Types (PROTOCOL.md "Types")
GW_BEACON, HELLO, CHALLENGE, LOGIN, WELCOME, ERR, RESEND, PING, PONG = (
    0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09)
BAL_REQ, BAL, COINS_REQ, COINS, SEND_REQ, OFFER, SIGN, SENT, BROADCAST = (
    0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18)
ADDR, ADDR_OK, TOK_REQ, TOKS = 0x19, 0x1A, 0x1B, 0x1C
PAGE_REQ, PAGE = 0x1D, 0x1E                              # the .mesh web (PROTOCOL.md, 2026-10-26)
# PAGE's status
PAGE_OK, PAGE_UNCHANGED, PAGE_MISSING = 0, 1, 2
PAGE_ZLIB = 0x80          # status bit: the body is zlib-compressed (only to a T-Deck whose PAGE_REQ said it reads it)
PAGE_ACCEPTS_ZLIB = 0x01  # PAGE_REQ's optional trailing flags byte
PIC_REQ, PIC = 0x1F, 0x24          # a picture on a .mesh page (PROTOCOL.md, 2026-10-26): a mesh picture, a piece at a time
PIC_OK, PIC_MISSING = 0, 2         # PIC's status
WHO_REQ, WHO, WATCH, WATCH_OK = 0x20, 0x21, 0x22, 0x23
MAIL_POST, MAIL_IN, MAIL_TOO_LONG, MAIL_ACK, MAIL_POLL = 0x30, 0x31, 0x32, 0x33, 0x34
PRESENCE, P2P_MSG, P2P_PAY, P2P_ACK = 0x40, 0x41, 0x42, 0x43

NAMES = {v: k for k, v in list(globals().items())
         if k.isupper() and isinstance(v, int) and 0 < v < 0x80 and k not in (
             "MAGIC", "HEADER", "MAX_FRAME", "MAX_BODY", "MAX_PARTS")}

# ERR codes
E_NOT_LOGGED_IN, E_BAD_REQUEST, E_ARCADE, E_BUSY, E_TOO_LONG, E_UNKNOWN_TAG, E_EXPIRED = range(1, 8)


class FrameError(ValueError):
    pass


@dataclass
class Message:
    type: int
    id: int
    body: bytes

    @property
    def name(self) -> str:
        return NAMES.get(self.type, f"0x{self.type:02x}")


def split(msg: Message) -> list[bytes]:
    """One message as the frames that go on air, in order."""
    chunks = [msg.body[i:i + MAX_BODY] for i in range(0, len(msg.body), MAX_BODY)] or [b""]
    if len(chunks) > MAX_PARTS:
        raise FrameError(f"{len(msg.body)} bytes is more than {MAX_PARTS} frames")
    count = len(chunks)
    return [struct.pack(">BBHB", MAGIC, msg.type, msg.id & 0xFFFF, (i << 4) | (count - 1)) + c
            for i, c in enumerate(chunks)]


def parse(frame: bytes) -> tuple[int, int, int, int, bytes]:
    """(type, id, index, count, chunk) of one frame."""
    if len(frame) < HEADER or frame[0] != MAGIC:
        raise FrameError("not an arcade frame")
    if len(frame) > MAX_FRAME:
        raise FrameError("frame too long")
    _, kind, ident, part = struct.unpack(">BBHB", frame[:HEADER])
    index, count = part >> 4, (part & 0x0F) + 1
    if index >= count:
        raise FrameError("part index past count")
    return kind, ident, index, count, frame[HEADER:]


def frames_needed(body_len: int) -> int:
    return max(1, -(-body_len // MAX_BODY))


class Joiner:
    """Collects parts per (sender, type, id) and yields whole messages."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.pending: dict[tuple, dict] = {}

    def add(self, sender, frame: bytes) -> Message | None:
        kind, ident, index, count, chunk = parse(frame)
        if count == 1:
            return Message(kind, ident, chunk)
        key = (sender, kind, ident)
        slot = self.pending.setdefault(key, {"count": count, "parts": {}, "at": self.clock()})
        if slot["count"] != count:
            raise FrameError("parts disagree on count")
        slot["parts"][index] = chunk
        if len(slot["parts"]) < count:
            return None
        del self.pending[key]
        return Message(kind, ident, b"".join(slot["parts"][i] for i in range(count)))

    def missing(self, sender, kind: int, ident: int) -> int:
        """Bitmap of parts still missing (for RESEND)."""
        slot = self.pending.get((sender, kind, ident))
        if not slot:
            return 0
        return sum(1 << i for i in range(slot["count"]) if i not in slot["parts"])

    def expire(self) -> list[tuple]:
        now = self.clock()
        gone = [k for k, s in self.pending.items() if now - s["at"] > JOIN_TIMEOUT]
        for k in gone:
            del self.pending[k]
        return gone


class Writer:
    def __init__(self):
        self.out = bytearray()

    def u8(self, v): self.out += struct.pack(">B", v); return self
    def u16(self, v): self.out += struct.pack(">H", v); return self
    def u32(self, v): self.out += struct.pack(">I", v); return self
    def amount(self, v): self.out += struct.pack(">Q", v & 0xFFFFFFFFFFFFFFFF); return self
    def raw(self, b): self.out += bytes(b); return self

    def str(self, s):
        b = (s or "").encode()
        if len(b) > 255:
            raise FrameError("string over 255 bytes")
        return self.u8(len(b)).raw(b)

    def bytes(self, b):
        if len(b) > 0xFFFF:
            raise FrameError("bytes over 65535")
        return self.u16(len(b)).raw(b)

    def done(self) -> bytes:
        return bytes(self.out)


class Reader:
    def __init__(self, body: bytes):
        self.body, self.at = body, 0

    def take(self, n) -> bytes:
        if self.at + n > len(self.body):
            raise FrameError("body ends early")
        b = self.body[self.at:self.at + n]
        self.at += n
        return b

    def u8(self): return self.take(1)[0]
    def u16(self): return struct.unpack(">H", self.take(2))[0]
    def u32(self): return struct.unpack(">I", self.take(4))[0]
    def amount(self): return struct.unpack(">Q", self.take(8))[0]
    def signed_amount(self): return struct.unpack(">q", self.take(8))[0]
    def str(self): return self.take(self.u8()).decode()
    def bytes(self): return self.take(self.u16())
    def rest(self): return self.take(len(self.body) - self.at)
    def end(self): return self.at == len(self.body)

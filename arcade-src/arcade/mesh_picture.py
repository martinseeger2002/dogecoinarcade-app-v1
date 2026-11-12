"""A picture on a .mesh page, made small for a T-Deck (2026-10-26: "Why can't we have small size images on .mesh
... like the old school Internet used to work where the image would load from the top to the bottom?").

Any image inscription can be put on a page (`![alt](inscription id)`, arcade/mesh_web.py). A browser shows the
inscription itself; a T-Deck is sent a MESH PICTURE made from it here, by the arcade its gateway asks:

    u8 version (1), u16 width, u16 height, u8 colours (1..16), colours x u16 RGB565,
    then bands to the bottom of the picture, each: u8 rows, u16 length, length bytes of zlib(the rows' pixels,
    two to a byte, high nibble first, each row starting on a byte)

All numbers big-endian. A band is made to fill about one LoRa part (BAND_BYTES), so as the parts of the answer come in,
in order, the picture draws from the top down -- the interlaced GIF of 1994, on a radio. At most MAX_W x MAX_H (never
made bigger), and at most PICTURE_MAX bytes in all: a bigger picture is drawn smaller until it fits.

Needs Pillow (the `lora` extra: only an arcade with a radio gateway makes these)."""
from __future__ import annotations

import io
import struct
import zlib

VERSION = 1
MAX_W, MAX_H = 300, 200
#: The most a picture costs by radio: about a minute at the gateway's pace (PROTOCOL.md), instant over WiFi.
PICTURE_MAX = 8192
#: A band's compressed bytes, at most: one LoRa part's body (195) less the answer's framing.
BAND_BYTES = 170
COLOURS = 16
#: What a transparent pixel becomes: the T-Deck's page background (#141310).
BACKGROUND = (0x14, 0x13, 0x10)


class PictureError(ValueError):
    pass


def rgb565(r: int, g: int, b: int) -> int:
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)


def _bands(pixels: bytes, width: int, height: int) -> bytes:
    """The rows, packed two pixels a byte, in bands of about BAND_BYTES compressed."""
    stride = (width + 1) // 2
    rows = []
    for y in range(height):
        row = pixels[y * width:(y + 1) * width]
        packed = bytearray(stride)
        for x, v in enumerate(row):
            packed[x >> 1] |= (v & 15) << (4 if x % 2 == 0 else 0)
        rows.append(bytes(packed))
    out, y = bytearray(), 0
    while y < height:
        n, best = 1, zlib.compress(rows[y], 9)
        while y + n < height and n < 255:
            trial = zlib.compress(b"".join(rows[y:y + n + 1]), 9)
            if len(trial) > BAND_BYTES:
                break
            n, best = n + 1, trial
        out += struct.pack(">BH", n, len(best)) + best
        y += n
    return bytes(out)


def make(image_bytes: bytes, limit: int = PICTURE_MAX) -> bytes:
    """A mesh picture of any image Pillow reads (PNG, GIF, JPEG, WebP, ...); PictureError when it is none."""
    try:
        from PIL import Image
    except ImportError as exc:                           # noqa: F841
        raise PictureError("this arcade cannot make pictures for a T-Deck (it needs Pillow: Admin -> Meshtastic)")
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.seek(0)                                      # an animation: its first frame
        img = img.convert("RGBA")
    except Exception as exc:                             # noqa: BLE001 -- anything Pillow cannot read
        raise PictureError(f"not a picture: {exc}") from None
    flat = Image.new("RGBA", img.size, BACKGROUND + (255,))
    flat.alpha_composite(img)
    flat = flat.convert("RGB")
    scale = min(1.0, MAX_W / flat.width, MAX_H / flat.height)
    small = max(flat.width, flat.height) <= 64          # pixel art: kept sharp
    for attempt in range(12):
        w, h = max(1, round(flat.width * scale)), max(1, round(flat.height * scale))
        sized = flat if (w, h) == flat.size else flat.resize((w, h), Image.NEAREST if small else Image.LANCZOS)
        exact = sized.getcolors(COLOURS)                 # 16 colours or fewer already: kept exactly, no dither
        for dither in ((Image.Dither.FLOYDSTEINBERG, Image.Dither.NONE) if not small and not exact
                       else (Image.Dither.NONE,)):
            if exact:
                q = Image.new("P", sized.size)
                q.putpalette([v for _n, rgb in exact for v in rgb])
                index = {rgb: i for i, (_n, rgb) in enumerate(exact)}
                raw = sized.tobytes()
                q.frombytes(bytes(index[tuple(raw[i:i + 3])] for i in range(0, len(raw), 3)))
            else:
                # median cut alone spends a photo's 16 colours on its loudest corners (a Doge came out pink);
                # a few k-means passes after it put them back where the picture is (2026-10-26)
                q = sized.quantize(colors=COLOURS, method=Image.Quantize.MEDIANCUT, kmeans=3, dither=dither)
            palette = q.getpalette()[:3 * COLOURS]
            used = max(q.tobytes()) + 1 if w * h else 1
            colours = b"".join(struct.pack(">H", rgb565(*palette[3 * i:3 * i + 3])) for i in range(used))
            body = struct.pack(">BHHB", VERSION, w, h, used) + colours + _bands(q.tobytes(), w, h)
            if len(body) <= limit:
                return body
        scale *= 0.8
    raise PictureError("that picture will not fit a T-Deck even drawn small")


def read(body: bytes) -> tuple[int, int, list[int], list[bytes]]:
    """(width, height, colours as RGB565, each row's pixel indexes): the T-Deck's reading, for tests and previews."""
    version, w, h, used = struct.unpack(">BHHB", body[:6])
    if version != VERSION:
        raise PictureError("not a mesh picture")
    colours = list(struct.unpack(f">{used}H", body[6:6 + 2 * used]))
    at, rows, stride = 6 + 2 * used, [], (w + 1) // 2
    while len(rows) < h:
        n, length = struct.unpack(">BH", body[at:at + 3])
        data = zlib.decompress(body[at + 3:at + 3 + length])
        for i in range(n):
            packed = data[i * stride:(i + 1) * stride]
            rows.append(bytes((packed[x >> 1] >> (4 if x % 2 == 0 else 0)) & 15 for x in range(w)))
        at += 3 + length
    return w, h, colours, rows

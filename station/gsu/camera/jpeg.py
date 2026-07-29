"""A baseline JPEG writer for flat 8x8 blocks, and nothing else.

**This exists for the synthetic camera and must not be used for a real one.**
It can only encode images made of uniform 8x8 tiles, because that is the one
case where the discrete cosine transform is free: a flat block has a DC
coefficient of `8 * (value - 128)` and every AC coefficient is exactly zero, so
there is no transform to run at all. That restriction is what makes a JPEG
encoder in pure Python defensible on a Raspberry Pi 2B — a general one would
cost hundreds of milliseconds a frame in CPython, which is far more than the
whole rest of the station's tick.

Real cameras never come near this file. `picamera2` and `rpicam-jpeg` both
produce JPEG in hardware and the station simply passes their bytes on.

What that restriction costs, stated plainly because it shows up in HARDWARE.md:

- The test card is a mosaic. It is meant to be — a generated frame should look
  generated — but it is not a photograph and does not compress like one.
- **Quality barely changes the file size.** The quantisation tables are written
  correctly and honoured by decoders, but a frame with no AC coefficients has
  almost nothing for them to discard. A real camera's `quality` parameter is the
  usual size/detail trade; the synthetic source's is close to a no-op, and the
  bandwidth figure to plan with is the one from a real camera.

The output is ordinary baseline JPEG — SOI, APP0/JFIF, DQT, SOF0, DHT, SOS,
entropy-coded data, EOI — with the standard Annex K Huffman tables and no
subsampling (4:4:4), so every 8x8 tile is one MCU of three blocks. Verified by
decoding the output with an independent decoder in `tests/test_video.py`.
"""

from __future__ import annotations

# --- quantisation, Annex K -----------------------------------------------

_LUMA_Q = (
    16, 11, 10, 16, 24, 40, 51, 61,
    12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56,
    14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77,
    24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101,
    72, 92, 95, 98, 112, 100, 103, 99,
)

_CHROMA_Q = (
    17, 18, 24, 47, 99, 99, 99, 99,
    18, 21, 26, 66, 99, 99, 99, 99,
    24, 26, 56, 99, 99, 99, 99, 99,
    47, 66, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
)

ZIGZAG = (
    0, 1, 8, 16, 9, 2, 3, 10,
    17, 24, 32, 25, 18, 11, 4, 5,
    12, 19, 26, 33, 40, 48, 41, 34,
    27, 20, 13, 6, 7, 14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36,
    29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46,
    53, 60, 61, 54, 47, 55, 62, 63,
)

# --- Huffman, Annex K.3 --------------------------------------------------

_DC_LUMA_BITS = (0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0)
_DC_LUMA_VALUES = tuple(range(12))
_DC_CHROMA_BITS = (0, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0)
_DC_CHROMA_VALUES = tuple(range(12))

_AC_LUMA_BITS = (0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 0x7D)
_AC_LUMA_VALUES = (
    0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12, 0x21, 0x31, 0x41, 0x06,
    0x13, 0x51, 0x61, 0x07, 0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xA1, 0x08,
    0x23, 0x42, 0xB1, 0xC1, 0x15, 0x52, 0xD1, 0xF0, 0x24, 0x33, 0x62, 0x72,
    0x82, 0x09, 0x0A, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x25, 0x26, 0x27, 0x28,
    0x29, 0x2A, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x43, 0x44, 0x45,
    0x46, 0x47, 0x48, 0x49, 0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59,
    0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6A, 0x73, 0x74, 0x75,
    0x76, 0x77, 0x78, 0x79, 0x7A, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
    0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A, 0xA2, 0xA3,
    0xA4, 0xA5, 0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6,
    0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9,
    0xCA, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA, 0xE1, 0xE2,
    0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xF1, 0xF2, 0xF3, 0xF4,
    0xF5, 0xF6, 0xF7, 0xF8, 0xF9, 0xFA,
)

_AC_CHROMA_BITS = (0, 2, 1, 2, 4, 4, 3, 4, 7, 5, 4, 4, 0, 1, 2, 0x77)
_AC_CHROMA_VALUES = (
    0x00, 0x01, 0x02, 0x03, 0x11, 0x04, 0x05, 0x21, 0x31, 0x06, 0x12, 0x41,
    0x51, 0x07, 0x61, 0x71, 0x13, 0x22, 0x32, 0x81, 0x08, 0x14, 0x42, 0x91,
    0xA1, 0xB1, 0xC1, 0x09, 0x23, 0x33, 0x52, 0xF0, 0x15, 0x62, 0x72, 0xD1,
    0x0A, 0x16, 0x24, 0x34, 0xE1, 0x25, 0xF1, 0x17, 0x18, 0x19, 0x1A, 0x26,
    0x27, 0x28, 0x29, 0x2A, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x43, 0x44,
    0x45, 0x46, 0x47, 0x48, 0x49, 0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58,
    0x59, 0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6A, 0x73, 0x74,
    0x75, 0x76, 0x77, 0x78, 0x79, 0x7A, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87,
    0x88, 0x89, 0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A,
    0xA2, 0xA3, 0xA4, 0xA5, 0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4,
    0xB5, 0xB6, 0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7,
    0xC8, 0xC9, 0xCA, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA,
    0xE2, 0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xF2, 0xF3, 0xF4,
    0xF5, 0xF6, 0xF7, 0xF8, 0xF9, 0xFA,
)


def _codes(bits: tuple[int, ...], values: tuple[int, ...]) -> dict[int, tuple[int, int]]:
    """Canonical Huffman codes: symbol → (code, length)."""
    table: dict[int, tuple[int, int]] = {}
    code = 0
    index = 0
    for length in range(1, 17):
        for _ in range(bits[length - 1]):
            table[values[index]] = (code, length)
            index += 1
            code += 1
        code <<= 1
    return table


_DC_LUMA = _codes(_DC_LUMA_BITS, _DC_LUMA_VALUES)
_DC_CHROMA = _codes(_DC_CHROMA_BITS, _DC_CHROMA_VALUES)
_AC_LUMA = _codes(_AC_LUMA_BITS, _AC_LUMA_VALUES)
_AC_CHROMA = _codes(_AC_CHROMA_BITS, _AC_CHROMA_VALUES)


def _concat(pairs: tuple[tuple[int, int], ...]) -> tuple[int, int]:
    """Join (code, length) pairs into one (code, length)."""
    code = 0
    total = 0
    for value, length in pairs:
        code = (code << length) | value
        total += length
    return code, total


#: A tile identical to the one before it encodes to a fixed 14 bits: three
#: zero DC differences and three end-of-block symbols, none of which depend on
#: anything. A test card is mostly such tiles, so they are written as a
#: precomputed pattern rather than reasoned about a symbol at a time — which is
#: most of the difference between this encoder costing 10 ms a frame and 3.
_REPEAT = _concat((
    _DC_LUMA[0], _AC_LUMA[0x00],
    _DC_CHROMA[0], _AC_CHROMA[0x00],
    _DC_CHROMA[0], _AC_CHROMA[0x00],
))
#: Four of them at a time: 56 bits, comfortably inside a machine word, and one
#: call to the bit writer instead of four.
_REPEAT_4 = _concat((_REPEAT,) * 4)


def quant_tables(quality: int) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """The two tables, scaled the way libjpeg scales them.

    Kept identical to the standard scaling so that a `quality` set here means
    the same thing as the same number given to a real camera — the numbers
    should not mean two different things in two halves of one station.
    """
    quality = max(1, min(100, int(quality)))
    scale = 5000 // quality if quality < 50 else 200 - quality * 2

    def scaled(table: tuple[int, ...]) -> tuple[int, ...]:
        return tuple(max(1, min(255, (value * scale + 50) // 100)) for value in table)

    return scaled(_LUMA_Q), scaled(_CHROMA_Q)


class _Bits:
    """Bit writer with the byte stuffing the format requires.

    A 0xFF in the entropy-coded data must be followed by 0x00, or a decoder
    reads it as a marker and the frame ends early — which would be a truncated
    picture that passes every completeness check the station has.
    """

    __slots__ = ("out", "_acc", "_n")

    def __init__(self) -> None:
        self.out = bytearray()
        self._acc = 0
        self._n = 0

    def write(self, code: int, length: int) -> None:
        self._acc = (self._acc << length) | (code & ((1 << length) - 1))
        self._n += length
        while self._n >= 8:
            self._n -= 8
            byte = (self._acc >> self._n) & 0xFF
            self.out.append(byte)
            if byte == 0xFF:
                self.out.append(0x00)
        self._acc &= (1 << self._n) - 1

    def flush(self) -> None:
        if self._n:
            self.write((1 << (8 - self._n)) - 1, 8 - self._n)  # pad with ones
        self._acc = 0
        self._n = 0


def _category(value: int) -> tuple[int, int]:
    """(size, bits) for a coefficient difference, per the standard."""
    if value == 0:
        return 0, 0
    size = abs(value).bit_length()
    return size, value if value > 0 else value + (1 << size) - 1


def _segment(marker: int, payload: bytes) -> bytes:
    return bytes((0xFF, marker)) + (len(payload) + 2).to_bytes(2, "big") + payload


def _headers(width: int, height: int, luma: tuple[int, ...],
             chroma: tuple[int, ...]) -> bytearray:
    out = bytearray(b"\xff\xd8")
    out += _segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
    for index, table in ((0, luma), (1, chroma)):
        out += _segment(0xDB, bytes((index,)) + bytes(table[ZIGZAG[i]] for i in range(64)))
    # SOF0: 8-bit, three components, no subsampling (0x11 = 1x1 sampling), so
    # one 8x8 tile is exactly one MCU of three blocks and there is no chroma
    # resampling to get wrong.
    out += _segment(
        0xC0,
        bytes((8,)) + height.to_bytes(2, "big") + width.to_bytes(2, "big") + bytes(
            (3, 1, 0x11, 0, 2, 0x11, 1, 3, 0x11, 1)
        ),
    )
    for class_id, bits, values in (
        (0x00, _DC_LUMA_BITS, _DC_LUMA_VALUES),
        (0x10, _AC_LUMA_BITS, _AC_LUMA_VALUES),
        (0x01, _DC_CHROMA_BITS, _DC_CHROMA_VALUES),
        (0x11, _AC_CHROMA_BITS, _AC_CHROMA_VALUES),
    ):
        out += _segment(0xC4, bytes((class_id,)) + bytes(bits) + bytes(values))
    out += _segment(0xDA, bytes((3, 1, 0x00, 2, 0x11, 3, 0x11, 0, 63, 0)))
    return out


def rgb_to_ycbcr(red: int, green: int, blue: int) -> tuple[int, int, int]:
    """JFIF full-range conversion. Integer, and clamped."""
    y = (299 * red + 587 * green + 114 * blue) // 1000
    cb = 128 + (-169 * red - 331 * green + 500 * blue) // 1000
    cr = 128 + (500 * red - 419 * green - 81 * blue) // 1000
    return (max(0, min(255, y)), max(0, min(255, cb)), max(0, min(255, cr)))


def encode_tiles(
    tiles: list[tuple[int, int, int]],
    tiles_x: int,
    tiles_y: int,
    width: int,
    height: int,
    quality: int = 75,
) -> bytes:
    """Encode a mosaic of flat 8x8 RGB tiles as one baseline JPEG.

    `tiles` is row-major, `tiles_x * tiles_y` long, and covers `ceil(width/8)`
    by `ceil(height/8)` tiles — the frame is padded out to whole blocks and the
    declared size crops it back, which is how every JPEG handles a size that is
    not a multiple of eight.

    Each tile's three quantised DC values are cached by colour, so a test card
    with a few dozen colours does that arithmetic a few dozen times rather than
    thousands of times a frame.
    """
    if len(tiles) != tiles_x * tiles_y:
        raise ValueError(
            f"expected {tiles_x * tiles_y} tiles for a {tiles_x}x{tiles_y} grid, "
            f"got {len(tiles)}"
        )
    luma, chroma = quant_tables(quality)
    ydiv, cdiv = luma[0], chroma[0]

    cache: dict[tuple[int, int, int], tuple[int, int, int]] = {}

    def quantised(colour: tuple[int, int, int]) -> tuple[int, int, int]:
        found = cache.get(colour)
        if found is None:
            y, cb, cr = rgb_to_ycbcr(*colour)
            # A flat block's DC coefficient is exactly 8 * (value - 128) and
            # every AC coefficient is exactly zero. That identity is the whole
            # reason this encoder can exist without a DCT.
            found = (
                round(8 * (y - 128) / ydiv),
                round(8 * (cb - 128) / cdiv),
                round(8 * (cr - 128) / cdiv),
            )
            cache[colour] = found
        return found

    bits = _Bits()
    write = bits.write
    previous = [0, 0, 0]
    tables = (
        (_DC_LUMA, _AC_LUMA),
        (_DC_CHROMA, _AC_CHROMA),
        (_DC_CHROMA, _AC_CHROMA),
    )
    repeat_code, repeat_length = _REPEAT
    repeat4_code, repeat4_length = _REPEAT_4

    index = 0
    count = len(tiles)
    while index < count:
        colour = tiles[index]
        run = index + 1
        while run < count and tiles[run] == colour:
            run += 1

        values = quantised(colour)
        for component in range(3):
            dc_table, ac_table = tables[component]
            diff = values[component] - previous[component]
            previous[component] = values[component]
            size, payload = _category(diff)
            code, length = dc_table[size]
            write(code, length)
            if size:
                write(payload, size)
            # Every AC coefficient is zero, so the block is one end-of-block
            # symbol. A general encoder would run-length the AC band here;
            # there is deliberately no path to reach that case.
            code, length = ac_table[0x00]
            write(code, length)

        # The rest of the run repeats the same nothing: zero DC difference and
        # an end-of-block for each component.
        repeats = run - index - 1
        while repeats >= 4:
            write(repeat4_code, repeat4_length)
            repeats -= 4
        while repeats:
            write(repeat_code, repeat_length)
            repeats -= 1
        index = run
    bits.flush()

    out = _headers(width, height, luma, chroma)
    out += bits.out
    out += b"\xff\xd9"
    return bytes(out)

"""A small, self-contained QR encoder -- enough for a Wi-Fi join code.

The sign shows a QR on its own screen so someone can point a phone at it and
join the network without being told the password. That has to work with no
internet and no pip on the playa, so this is pure Python with no dependency --
in the spirit of the rest of the box (the 5x7 font, the RTC ioctls). It is
verified byte-for-byte against the `segno` library in the tests, so "small"
does not mean "approximately a QR code": the matrices are identical.

Scope is deliberately narrow: **byte mode, versions 1-10**. A Wi-Fi join
string is short (an SSID, a passphrase, a few field markers), and version 10
byte mode holds 271 characters even at the lowest error correction -- far more
than any passphrase. Numeric and alphanumeric modes would pack denser but buy
nothing here, so they are left out.

``encode(text)`` returns the module matrix as a list of rows of 0/1, quiet
zone included. The caller draws it -- pygame rectangles on the touchscreen, a
grid on the phone -- from that one matrix, so both surfaces show the same code
and neither re-implements any of this.
"""

from __future__ import annotations

# ------------------------------------------------------------------ GF(256)
#
# Reed-Solomon works in the Galois field GF(256) with the QR generator
# polynomial 0x11d. These log/antilog tables turn its multiplications into
# additions, which is the whole point of using them.
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11d
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _rs_generator(degree: int) -> list:
    """The generator polynomial for `degree` error-correction codewords."""
    poly = [1]
    for i in range(degree):
        # Multiply by (x - alpha^i).
        nxt = [0] * (len(poly) + 1)
        for j, coeff in enumerate(poly):
            nxt[j] ^= coeff
            nxt[j + 1] ^= _gf_mul(coeff, _EXP[i])
        poly = nxt
    return poly


def _rs_encode(data: list, degree: int) -> list:
    """The `degree` ECC codewords for a block of data codewords."""
    gen = _rs_generator(degree)
    remainder = list(data) + [0] * degree
    for i in range(len(data)):
        factor = remainder[i]
        if factor == 0:
            continue
        for j, coeff in enumerate(gen):
            remainder[i + j] ^= _gf_mul(coeff, factor)
    return remainder[len(data):]


# -------------------------------------------------------- version/ECC tables
#
# Straight from ISO/IEC 18004, versions 1-10. For each (version, level):
#   (ec_per_block, [(block_count, data_codewords_per_block), ...])
# The block groups reproduce the standard's "number of blocks in group 1/2".
_ECC = {
    "L": {
        1: (7, [(1, 19)]), 2: (10, [(1, 34)]), 3: (15, [(1, 55)]),
        4: (20, [(1, 80)]), 5: (26, [(1, 108)]), 6: (18, [(2, 68)]),
        7: (20, [(2, 78)]), 8: (24, [(2, 97)]), 9: (30, [(2, 116)]),
        10: (18, [(2, 68), (2, 69)]),
    },
    "M": {
        1: (10, [(1, 16)]), 2: (16, [(1, 28)]), 3: (26, [(1, 44)]),
        4: (18, [(2, 32)]), 5: (24, [(2, 43)]), 6: (16, [(4, 27)]),
        7: (18, [(4, 31)]), 8: (22, [(2, 38), (2, 39)]),
        9: (22, [(3, 36), (2, 37)]), 10: (26, [(4, 43), (1, 44)]),
    },
    "Q": {
        1: (13, [(1, 13)]), 2: (22, [(1, 22)]), 3: (18, [(2, 17)]),
        4: (26, [(2, 24)]), 5: (18, [(2, 15), (2, 16)]),
        6: (24, [(4, 19)]), 7: (18, [(2, 14), (4, 15)]),
        8: (22, [(4, 18), (2, 19)]), 9: (20, [(4, 16), (4, 17)]),
        10: (24, [(6, 19), (2, 20)]),
    },
    "H": {
        1: (17, [(1, 9)]), 2: (28, [(1, 16)]), 3: (22, [(2, 13)]),
        4: (16, [(4, 9)]), 5: (22, [(2, 11), (2, 12)]),
        6: (28, [(4, 15)]), 7: (26, [(4, 13), (1, 14)]),
        8: (26, [(4, 14), (2, 15)]), 9: (24, [(4, 12), (4, 13)]),
        10: (28, [(6, 15), (2, 16)]),
    },
}

# Alignment-pattern centre coordinates by version (the finder-pattern corners
# are handled separately). Empty for version 1.
_ALIGN = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
    7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
}


def _data_capacity(version: int, level: str) -> int:
    """Total data codewords available at this version and ECC level."""
    ec_per, groups = _ECC[level][version]
    return sum(count * dcw for count, dcw in groups)


# ------------------------------------------------------------------ encoding

def _byte_bits(data: bytes, version: int) -> list:
    """The bitstream for a byte-mode message: mode, length, payload."""
    bits = []

    def push(value, length):
        for i in range(length - 1, -1, -1):
            bits.append((value >> i) & 1)

    push(0b0100, 4)                              # byte mode
    # Character-count indicator: 8 bits for v1-9, 16 for v10-26 in byte mode.
    push(len(data), 8 if version <= 9 else 16)
    for byte in data:
        push(byte, 8)
    return bits


def _fill_codewords(bits: list, capacity: int) -> list:
    """Terminator, bit-padding to a byte boundary, then the pad bytes."""
    # Up to four zero bits of terminator, but no more than the room left.
    bits = list(bits) + [0] * min(4, capacity * 8 - len(bits))
    if len(bits) % 8:
        bits += [0] * (8 - len(bits) % 8)
    codewords = [int("".join(str(b) for b in bits[i:i + 8]), 2)
                 for i in range(0, len(bits), 8)]
    # The specified pad pattern ALTERNATES 11101100 / 00010001 to the end.
    pad = (0xEC, 0x11)
    while len(codewords) < capacity:
        codewords.append(pad[(len(codewords) - len(bits) // 8) % 2])
    return codewords[:capacity]


def _interleave(codewords: list, version: int, level: str) -> list:
    """Split into blocks, add ECC, and interleave data then ECC per the spec."""
    ec_per, groups = _ECC[level][version]
    blocks = []
    pos = 0
    for count, dcw in groups:
        for _ in range(count):
            data = codewords[pos:pos + dcw]
            pos += dcw
            blocks.append((data, _rs_encode(data, ec_per)))

    result = []
    max_data = max(len(d) for d, _ in blocks)
    for i in range(max_data):
        for data, _ in blocks:
            if i < len(data):
                result.append(data[i])
    for i in range(ec_per):
        for _, ecc in blocks:
            result.append(ecc[i])
    return result


def _choose_version(length: int, level: str) -> int:
    for version in range(1, 11):
        header = 4 + (8 if version <= 9 else 16)
        if header + length * 8 <= _data_capacity(version, level) * 8:
            return version
    raise ValueError("message too long for a version-10 QR (%d bytes)" % length)


# ------------------------------------------------------------ matrix layout

def _new_matrix(size: int):
    """A grid of None (unset), so function patterns can be told from data."""
    return [[None] * size for _ in range(size)]


def _place_finders(m, size):
    for r0, c0 in ((0, 0), (0, size - 7), (size - 7, 0)):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = r0 + dr, c0 + dc
                if not (0 <= r < size and 0 <= c < size):
                    continue
                if dr in (-1, 7) or dc in (-1, 7):
                    m[r][c] = 0                  # separator ring
                elif dr in (0, 6) or dc in (0, 6):
                    m[r][c] = 1                  # outer square
                elif 2 <= dr <= 4 and 2 <= dc <= 4:
                    m[r][c] = 1                  # centre 3x3
                else:
                    m[r][c] = 0


def _place_timing(m, size):
    for i in range(8, size - 8):
        bit = 1 - (i & 1)
        if m[6][i] is None:
            m[6][i] = bit
        if m[i][6] is None:
            m[i][6] = bit


def _place_alignment(m, size, version):
    centres = _ALIGN[version]
    for r in centres:
        for c in centres:
            # Skip only the three finder corners. Patterns that land on the
            # timing line (versions 7+ put centres at row/col 6) are real and
            # overwrite the timing there -- an earlier "already set, skip"
            # guard here silently dropped them and broke every version >= 7.
            if (r, c) in ((6, 6), (6, size - 7), (size - 7, 6)):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    ring = max(abs(dr), abs(dc))
                    m[r + dr][c + dc] = 1 if ring != 1 else 0


_VERSION_POLY = 0x1f25


def _place_version(m, size, version):
    """The 18-bit version block that versions 7+ carry by the finders.

    Six version bits plus twelve BCH parity, laid in a 3x6 grid above the
    bottom-left finder and its transpose left of the top-right finder. Absent
    below version 7, which is why a v6 code has nothing here.
    """
    if version < 7:
        return
    rem = version << 12
    for i in range(5, -1, -1):
        if rem & (1 << (12 + i)):
            rem ^= _VERSION_POLY << i
    value = (version << 12) | rem                 # 18 bits, d17..d0
    for i in range(18):
        bit = (value >> i) & 1                     # d0 first
        r, c = i // 3, size - 11 + i % 3
        m[r][c] = bit                              # top-right block
        m[c][r] = bit                              # bottom-left (transpose)


def _reserve_format(m, size):
    """Mark the format/version areas as taken so data skips them."""
    for i in range(9):
        if m[8][i] is None:
            m[8][i] = 0
        if m[i][8] is None:
            m[i][8] = 0
    for i in range(8):
        m[8][size - 1 - i] = 0
        m[size - 1 - i][8] = 0
    m[size - 8][8] = 1                            # the dark module


def _data_positions(size):
    """The zigzag of data-module coordinates, right to left, bottom to top."""
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:                             # skip the timing column
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for r in rows:
            for c in (col, col - 1):
                yield r, c
        upward = not upward
        col -= 2


_MASKS = (
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
)

_FORMAT_POLY = 0b10100110111
_FORMAT_MASK = 0b101010000010010
_ECC_INDICATOR = {"L": 0b01, "M": 0b00, "Q": 0b11, "H": 0b10}


def _format_bits(level: str, mask: int) -> list:
    data = (_ECC_INDICATOR[level] << 3) | mask
    rem = data << 10
    for i in range(4, -1, -1):
        if rem & (1 << (10 + i)):
            rem ^= _FORMAT_POLY << i
    bits = ((data << 10) | rem) ^ _FORMAT_MASK
    return [(bits >> i) & 1 for i in range(14, -1, -1)]


def _place_format(m, size, level, mask):
    bits = _format_bits(level, mask)
    # Copy one: around the top-left finder.
    coords = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8),
              (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]
    for bit, (r, c) in zip(bits, coords):
        m[r][c] = bit
    # Copy two: split across the other two finders, and the dark module.
    coords2 = [(size - 1, 8), (size - 2, 8), (size - 3, 8), (size - 4, 8),
               (size - 5, 8), (size - 6, 8), (size - 7, 8),
               (8, size - 8), (8, size - 7), (8, size - 6), (8, size - 5),
               (8, size - 4), (8, size - 3), (8, size - 2), (8, size - 1)]
    for bit, (r, c) in zip(bits, coords2):
        m[r][c] = bit


def _penalty(grid, size) -> int:
    """The standard four penalty rules; lower is a better-looking mask."""
    score = 0
    # Rule 1: runs of five or more same-colour modules in a row/column.
    for line in (grid, list(zip(*grid))):
        for row in line:
            run = 1
            for i in range(1, size):
                if row[i] == row[i - 1]:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + (run - 5)
                    run = 1
            if run >= 5:
                score += 3 + (run - 5)
    # Rule 2: 2x2 blocks of one colour.
    for r in range(size - 1):
        for c in range(size - 1):
            if grid[r][c] == grid[r][c + 1] == grid[r + 1][c] == grid[r + 1][c + 1]:
                score += 3
    # Rule 3: the finder-like 1:1:3:1:1 pattern, with four light modules beside.
    patterns = ([1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0], [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1])
    for line in (grid, list(zip(*grid))):
        for row in line:
            row = list(row)
            for i in range(size - 10):
                if row[i:i + 11] in patterns:
                    score += 40
    # Rule 4: overall dark/light balance.
    dark = sum(sum(row) for row in grid)
    ratio = dark * 100 // (size * size)
    score += 10 * (abs(ratio - 50) // 5)
    return score


def _matrix_for_mask(base, reserved, size, level, data_bits, mask):
    """Lay the data with one mask applied and return the finished grid."""
    grid = [row[:] for row in base]
    it = iter(data_bits)
    for r, c in _data_positions(size):
        if reserved[r][c]:
            continue
        try:
            bit = next(it)
        except StopIteration:
            bit = 0
        if _MASKS[mask](r, c):
            bit ^= 1
        grid[r][c] = bit
    _place_format(grid, size, level, mask)
    return grid


def encode(text, level: str = "M") -> list:
    """The QR module matrix for `text`, quiet zone included.

    Returns a list of rows of 0 (light) and 1 (dark). ``level`` is the error
    correction level L/M/Q/H; M is a sound default and what a phone camera
    expects. Version and mask are chosen automatically -- the version from the
    message length, the mask by the standard penalty score.
    """
    if level not in _ECC:
        raise ValueError("ecc level must be one of L, M, Q, H")
    data = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    version = _choose_version(len(data), level)
    size = version * 4 + 17

    bits = _byte_bits(data, version)
    codewords = _fill_codewords(bits, _data_capacity(version, level))
    data_bits = []
    for cw in _interleave(codewords, version, level):
        data_bits.extend((cw >> i) & 1 for i in range(7, -1, -1))
    # Remainder bits: some versions need a few zero bits past the last codeword.
    data_bits += [0] * (size * size)

    base = _new_matrix(size)
    _place_finders(base, size)
    _place_timing(base, size)
    _place_alignment(base, size, version)
    _place_version(base, size, version)
    _reserve_format(base, size)
    reserved = [[cell is not None for cell in row] for row in base]

    best_grid, best_score = None, None
    for mask in range(8):
        grid = _matrix_for_mask(base, reserved, size, level, data_bits, mask)
        score = _penalty(grid, size)
        if best_score is None or score < best_score:
            best_grid, best_score = grid, score

    quiet = 4
    out = []
    for _ in range(quiet):
        out.append([0] * (size + quiet * 2))
    for row in best_grid:
        out.append([0] * quiet + list(row) + [0] * quiet)
    for _ in range(quiet):
        out.append([0] * (size + quiet * 2))
    return out


# --------------------------------------------------------------- Wi-Fi join

def _escape(value: str) -> str:
    """Backslash-escape the characters the Wi-Fi QR grammar reserves."""
    out = []
    for ch in value:
        if ch in "\\;,:\"":
            out.append("\\")
        out.append(ch)
    return "".join(out)


def wifi_payload(ssid: str, password: str = "", security: str = "WPA",
                 hidden: bool = False) -> str:
    """The `WIFI:...;;` string a phone camera reads to join a network.

    `security` is WPA (covers WPA/WPA2/WPA3-PSK), WEP, or nopass for an open
    network. An empty password forces nopass -- a QR that claims WPA with no
    key just fails silently on the phone.
    """
    security = (security or "nopass").upper()
    if not password:
        security = "NOPASS"
    parts = ["WIFI:", "S:%s;" % _escape(ssid)]
    if security == "NOPASS":
        parts.append("T:nopass;")
    else:
        parts.append("T:%s;" % security)
        parts.append("P:%s;" % _escape(password))
    if hidden:
        parts.append("H:true;")
    parts.append(";")
    return "".join(parts)


def wifi_qr(ssid: str, password: str = "", security: str = "WPA",
            hidden: bool = False, level: str = "M") -> list:
    """The module matrix for a Wi-Fi join code."""
    return encode(wifi_payload(ssid, password, security, hidden), level=level)

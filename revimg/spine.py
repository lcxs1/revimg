# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 revimg contributors

"""Page layer: self-framing, Reed-Solomon protected symbols on the block grid.

Geometry
--------
The canvas is tiled by 8x8 pixel blocks; each block carries two QIM bits (see
`qim`).  A *page* is a 16x16 block square (128x128 canvas pixels) holding
512 bits = 64 bytes.

Framing
-------
    bits 0..15    sync word (key-derived, plaintext, checked before any decoding)
    bits 16..511  one 62-byte RS codeword:
                      byte 0-1    K   (fountain blocks)
                      byte 2-3    symbol index
                      byte 4      page x  (absolute page column in the canvas)
                      byte 5      page y
                      byte 6-34   payload (29 bytes)
                      byte 35-36  CRC-16/CCITT of bytes 0..34

The sync word matters: it is readable *without* error correction, so the
decoder can find the 16x16 page framing by comparing 16 bits instead of paying
for an RS decode at every one of 256 candidate framings.  It also means a page
that is damaged still validates its framing, which is what keeps the erasure
map fine-grained rather than all-or-nothing.

RS(62, 37+25) corrects up to 12 corrupted bytes.  Two independent integrity
checks (RS + CRC) mean a page that validates is almost certainly intact, and a
page that does not is simply an erasure for the fountain code.

Carrying the absolute page coordinates is what makes cropping recoverable: a
page that decodes says exactly where it lives in the original canvas, so the
decoder derives the crop offset with no external hint.
"""

from __future__ import annotations

import numpy as np

from . import gf256

BLOCK = 8
BITS_PER_BLOCK = 2
PAGE_BLOCKS = 16
PAGE_PX = BLOCK * PAGE_BLOCKS                            # 128
PAGE_BITS = PAGE_BLOCKS * PAGE_BLOCKS * BITS_PER_BLOCK    # 512

SYNC_BITS = 16
CODE_BYTES = (PAGE_BITS - SYNC_BITS) // 8                # 62
NSYM = 25                                                # corrects 12 bytes
MSG_BYTES = CODE_BYTES - NSYM                            # 37
SYM_DATA = 29

_OFF_K = 0
_OFF_IDX = 2
_OFF_PX = 4
_OFF_PY = 5
_OFF_DATA = 6


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def make_message(k: int, idx: int, payload: bytes, px: int, py: int) -> bytes:
    if len(payload) > SYM_DATA:
        raise ValueError("symbol payload too large")
    body = bytearray(MSG_BYTES - 2)
    body[_OFF_K:_OFF_K + 2] = int(k).to_bytes(2, "big")
    body[_OFF_IDX:_OFF_IDX + 2] = int(idx).to_bytes(2, "big")
    body[_OFF_PX] = int(px) & 0xFF
    body[_OFF_PY] = int(py) & 0xFF
    body[_OFF_DATA:_OFF_DATA + SYM_DATA] = payload.ljust(SYM_DATA, b"\x00")
    return bytes(body) + crc16(bytes(body)).to_bytes(2, "big")


def parse_message(msg: bytes):
    if len(msg) != MSG_BYTES:
        return None
    if crc16(msg[:-2]) != int.from_bytes(msg[-2:], "big"):
        return None
    k = int.from_bytes(msg[_OFF_K:_OFF_K + 2], "big")
    if k <= 0:
        return None
    idx = int.from_bytes(msg[_OFF_IDX:_OFF_IDX + 2], "big")
    payload = msg[_OFF_DATA:_OFF_DATA + SYM_DATA]
    return k, idx, payload, msg[_OFF_PX], msg[_OFF_PY]


def encode_page(k: int, idx: int, payload: bytes, px: int, py: int,
                sync: int = 0) -> np.ndarray:
    """-> (16,16,2) uint8 bit block for one page."""
    cw = gf256.rs_encode(make_message(k, idx, payload, px, py), NSYM)
    body = np.unpackbits(np.frombuffer(cw, dtype=np.uint8))
    head = np.unpackbits(np.frombuffer(int(sync).to_bytes(2, "big"), dtype=np.uint8))
    return np.concatenate([head, body]).reshape(PAGE_BLOCKS, PAGE_BLOCKS, BITS_PER_BLOCK)


def sync_ok(blk: np.ndarray, sync: int) -> bool:
    head = np.packbits(np.asarray(blk, dtype=np.uint8).reshape(-1)[:SYNC_BITS])
    return int.from_bytes(head.tobytes(), "big") == int(sync)


def decode_page(blk: np.ndarray, sync: int = 0):
    flat = np.asarray(blk, dtype=np.uint8).reshape(-1)
    if flat.size != PAGE_BITS or not sync_ok(blk, sync):
        return None
    cw = np.packbits(flat[SYNC_BITS:]).tobytes()
    fixed = gf256.rs_decode(cw, NSYM)
    if fixed is None:
        return None
    return parse_message(fixed[:MSG_BYTES])


class Layout:
    """Page grid on the canvas block grid."""

    def __init__(self, canvas_w: int, canvas_h: int):
        self.canvas_w = canvas_w
        self.canvas_h = canvas_h
        self.blocks_x = canvas_w // BLOCK
        self.blocks_y = canvas_h // BLOCK
        self.pages_x = self.blocks_x // PAGE_BLOCKS
        self.pages_y = self.blocks_y // PAGE_BLOCKS
        self.n_slots = self.pages_x * self.pages_y

    def slot_xy(self, slot: int):
        py, px = divmod(slot, self.pages_x)
        return px, py

    def build_bits(self, pages: dict) -> np.ndarray:
        bits = np.zeros((self.blocks_y, self.blocks_x, BITS_PER_BLOCK), dtype=np.uint8)
        if self.n_slots == 0:
            return bits
        grid = np.zeros((self.pages_y, PAGE_BLOCKS, self.pages_x, PAGE_BLOCKS, BITS_PER_BLOCK),
                        dtype=np.uint8)
        for slot, page in pages.items():
            py, px = divmod(int(slot), self.pages_x)
            grid[py, :, px, :, :] = page
        region = bits[: self.pages_y * PAGE_BLOCKS, : self.pages_x * PAGE_BLOCKS]
        region[...] = grid.reshape(self.pages_y * PAGE_BLOCKS,
                                   self.pages_x * PAGE_BLOCKS, BITS_PER_BLOCK)
        return bits

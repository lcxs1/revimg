# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 revimg contributors

"""Systematic random-linear fountain code over GF(256).

Why this and not plain Reed-Solomon: the channel here is an *erasure* channel
whose erasure rate is unknown at encode time (a crop or a scribble may remove
anywhere from 0% to 90% of the image).  A rateless code lets the decoder stop
as soon as it has enough symbols, so we can trade capacity for redundancy
without guessing the attack.

The code is systematic: symbols 0..K-1 are the raw blocks, the rest are random
GF(256) linear combinations.  Coefficient vectors are regenerated from
(seed, symbol index), so no coefficient data is transmitted.  Decoding is
Gaussian elimination over the blocks that are still missing; a set of s raw
blocks plus r generic dense rows spans min(K, s + r) dimensions with
overwhelming probability, so *any* K received symbols decode.
"""

from __future__ import annotations

import numpy as np

from . import gf256

_EXP = np.frombuffer(bytes(gf256.EXP), dtype=np.uint8)
_LOG = np.frombuffer(bytes(gf256.LOG), dtype=np.uint8)


def gf_mul_arr(a, b):
    """Elementwise GF(256) product of two uint8 arrays (broadcastable)."""
    a = np.asarray(a, dtype=np.uint8)
    b = np.asarray(b, dtype=np.uint8)
    # Widen before adding: uint8 + uint8 would wrap at 256 and corrupt the logs.
    la = _LOG[a.astype(np.int16)].astype(np.int16)
    lb = _LOG[b.astype(np.int16)].astype(np.int16)
    out = _EXP[(la + lb) % 255]
    out = np.where((a == 0) | (b == 0), np.uint8(0), out).astype(np.uint8)
    return out


def _coeffs(seed: int, idx: int, k: int) -> np.ndarray:
    rng = np.random.Generator(np.random.PCG64([int(seed) & 0xFFFFFFFF, int(idx)]))
    c = rng.integers(0, 256, size=k, dtype=np.int64).astype(np.uint8)
    return c


def _rng(seed: int) -> np.random.Generator:
    return np.random.Generator(np.random.PCG64(int(seed) & 0xFFFFFFFFFFFFFFFF))


def split(data: bytes, k: int, bs: int) -> np.ndarray:
    need = k * bs
    if len(data) > need:
        raise ValueError(f"payload {len(data)}B does not fit {k} x {bs}B blocks")
    buf = bytes(data) + b"\x00" * (need - len(data))
    return np.frombuffer(buf, dtype=np.uint8).reshape(k, bs).copy()


def join(blocks: np.ndarray, length: int) -> bytes:
    return blocks.reshape(-1).tobytes()[:length]


def plan(k: int, alpha: float) -> int:
    """Total symbols to place for a given redundancy factor alpha."""
    return int(k + max(4, round(k * (alpha - 1.0))))


def encode(blocks: np.ndarray, n_total: int, seed: int):
    """Yield (symbol_index, chunk_bytes) for n_total symbols."""
    k, bs = blocks.shape
    for idx in range(n_total):
        if idx < k:
            yield idx, blocks[idx].tobytes()
        else:
            c = _coeffs(seed, idx, k)
            chunk = np.bitwise_xor.reduce(gf_mul_arr(c[:, None], blocks), axis=0)
            yield idx, chunk.astype(np.uint8).tobytes()


def make_symbol(blocks: np.ndarray, idx: int, seed: int) -> bytes:
    k, _ = blocks.shape
    if idx < k:
        return blocks[idx].tobytes()
    c = _coeffs(seed, idx, k)
    chunk = np.bitwise_xor.reduce(gf_mul_arr(c[:, None], blocks), axis=0)
    return chunk.astype(np.uint8).tobytes()


def decode(received: dict, k: int, bs: int, seed: int):
    """received: {symbol_index: chunk_bytes}.  Returns blocks (k,bs) or None."""
    known = {}
    for i, chunk in received.items():
        if i < k and len(chunk) == bs:
            known[i] = np.frombuffer(chunk, dtype=np.uint8).copy()
    if len(known) == k:
        out = np.zeros((k, bs), dtype=np.uint8)
        for i, blk in known.items():
            out[i] = blk
        return out
    missing = [i for i in range(k) if i not in known]
    if not missing:
        return None
    rows = []
    rhs = []
    known_idx = np.fromiter(known.keys(), dtype=np.int64, count=len(known))
    known_mat = np.stack([known[i] for i in known_idx], axis=0) if len(known) else None
    for i, chunk in sorted(received.items()):
        if i < k or len(chunk) != bs:
            continue
        c = _coeffs(seed, i, k)
        r = np.frombuffer(chunk, dtype=np.uint8).copy()
        if known_mat is not None:
            acc = np.bitwise_xor.reduce(
                gf_mul_arr(c[known_idx][:, None], known_mat), axis=0
            )
            r = np.bitwise_xor(r, acc.astype(np.uint8))
        rows.append(c[np.asarray(missing, dtype=np.int64)])
        rhs.append(r)
    if len(rows) < len(missing):
        return None
    A = np.stack(rows, axis=0)
    B = np.stack(rhs, axis=0)
    solved = _gauss(A, B)
    if solved is None:
        return None
    out = np.zeros((k, bs), dtype=np.uint8)
    for i, blk in known.items():
        out[i] = blk
    for j, col in enumerate(missing):
        out[col] = solved[j]
    return out


def _gauss(A: np.ndarray, B: np.ndarray):
    """Solve A X = B over GF(256).  A (m,n), B (m,bs).  Returns (n,bs) or None."""
    m, n = A.shape
    A = A.copy()
    B = B.copy()
    row = 0
    pivot_row_of_col = {}
    for col in range(n):
        nz = np.nonzero(A[row:, col])[0]
        if nz.size == 0:
            continue
        p = row + int(nz[0])
        if p != row:
            A[[row, p]] = A[[p, row]]
            B[[row, p]] = B[[p, row]]
        piv = A[row, col]
        if piv != 1:
            inv = np.uint8(gf256.gf_inv(int(piv)))
            A[row] = gf_mul_arr(A[row], np.full(n, inv, dtype=np.uint8))
            B[row] = gf_mul_arr(B[row], np.full(B.shape[1], inv, dtype=np.uint8))
        pivot_row_of_col[col] = row
        below = np.nonzero(A[row + 1:, col])[0]
        if below.size:
            rows_idx = row + 1 + below
            factors = A[rows_idx, col].copy()
            # NB: A[rows_idx] is a copy under fancy indexing, so assign back.
            A[rows_idx] = A[rows_idx] ^ gf_mul_arr(factors[:, None], A[row][None, :])
            B[rows_idx] = B[rows_idx] ^ gf_mul_arr(factors[:, None], B[row][None, :])
        row += 1
        if row == m:
            break
    if len(pivot_row_of_col) < n:
        return None
    # back substitution
    X = np.zeros((n, B.shape[1]), dtype=np.uint8)
    for col in range(n - 1, -1, -1):
        if col not in pivot_row_of_col:
            return None
        r = pivot_row_of_col[col]
        acc = B[r].copy()
        later = [c for c in range(col + 1, n) if c in pivot_row_of_col]
        if later:
            coeff = A[r, later]
            nz = coeff != 0
            if np.any(nz):
                acc ^= np.bitwise_xor.reduce(
                    gf_mul_arr(coeff[nz][:, None], X[np.asarray(later)[nz]]), axis=0
                ).astype(np.uint8)
        X[col] = acc
    return X

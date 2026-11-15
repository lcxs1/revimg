# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 revimg contributors

"""Quantisation-index-modulation (QIM) carrier on 8x8 DCT blocks.

Each 8x8 block carries `len(COEFFS)` bits, one per low-frequency DCT
coefficient.  A bit is written by *dither modulation*: the coefficient is moved
to the nearest point of one of two interleaved lattices (spacing `delta`, the
"1" lattice shifted by delta/2).  Recovery is a nearest-lattice decision, so a
coefficient may drift by up to delta/4 before the bit flips -- which is what
makes the layer survive JPEG quantisation, mild resampling and noise.

Why low-frequency coefficients: JPEG's quantisation table is smallest there, and
resampling preserves low frequencies best.  Why DCT and not LSB: a single JPEG
re-encode zeroes the LSB plane, so LSB carries nothing here.
"""

from __future__ import annotations

import numpy as np

BLOCK = 8
#: Lowest AC coefficients: one cycle per block.  They survive JPEG's small
#: quantisation steps there *and* survive a 2x downscale, whereas the 2-cycle
#: coefficients sit exactly at the Nyquist frequency of a half-size copy.
COEFFS = ((0, 1), (1, 0))
QUALITY = 20          # JPEG quality whose quantisation grid the lattice matches

# JPEG luma quantisation table (ITU T.81 Annex K) -- used to choose QIM lattices
# that coincide with the JPEG lattice, which makes a JPEG round trip lossless for
# the carrier coefficients.
JPEG_LUMA = (
    (16, 11, 10, 16, 24, 40, 51, 61),
    (12, 12, 14, 19, 26, 58, 60, 55),
    (14, 13, 16, 24, 40, 57, 69, 56),
    (14, 17, 22, 29, 51, 87, 80, 62),
    (18, 22, 37, 56, 68, 109, 103, 77),
    (24, 35, 55, 64, 81, 104, 113, 92),
    (49, 64, 78, 87, 103, 121, 120, 101),
    (72, 92, 95, 98, 112, 100, 103, 99),
)


def jpeg_step(u: int, v: int, quality: int) -> int:
    s = 5000.0 / quality if quality < 50 else 200.0 - 2.0 * quality
    return max(1, int((JPEG_LUMA[u][v] * s + 50) / 100))


def deltas_for(quality: int) -> tuple:
    """QIM step per coefficient that lines up with a JPEG re-encode at `quality`."""
    return tuple(2.0 * jpeg_step(u, v, quality) for u, v in COEFFS)


#: Lattice steps tuned so that a JPEG re-encode at QUALITY is (nearly) lossless
#: for the carrier coefficients: every QIM lattice point is an exact multiple of
#: the JPEG quantisation step, so quantise/dequantise maps it to itself.
DEFAULT_DELTA = deltas_for(QUALITY)


def _dct_matrix(n: int = BLOCK) -> np.ndarray:
    k = np.arange(n)[:, None]
    x = np.arange(n)[None, :]
    d = np.cos(np.pi * (2 * x + 1) * k / (2.0 * n)) * np.sqrt(2.0 / n)
    d[0, :] = np.sqrt(1.0 / n)
    return d.astype(np.float32)


D = _dct_matrix()


def to_blocks(a: np.ndarray) -> np.ndarray:
    """(H,W) -> (nh,nw,8,8).  H and W must be multiples of 8."""
    h, w = a.shape
    return a.reshape(h // 8, 8, w // 8, 8).transpose(0, 2, 1, 3)


def dct2(a: np.ndarray) -> np.ndarray:
    b = to_blocks(a).astype(np.float32)
    return np.matmul(np.matmul(D, b), D.T)


def idct2(c: np.ndarray) -> np.ndarray:
    b = np.matmul(np.matmul(D.T, c), D)
    nh, nw = b.shape[:2]
    return b.transpose(0, 2, 1, 3).reshape(nh * 8, nw * 8)


def _deltas(delta) -> tuple:
    if np.isscalar(delta):
        return tuple(float(delta) for _ in COEFFS)
    d = tuple(float(x) for x in delta)
    if len(d) != len(COEFFS):
        raise ValueError("need one delta per coefficient")
    return d


def qim_point(coef: np.ndarray, bit: np.ndarray, delta: float) -> np.ndarray:
    off = bit.astype(np.float32) * (delta * 0.5)
    return np.round((coef - off) / delta) * delta + off


def embed(luma: np.ndarray, bits: np.ndarray, delta=DEFAULT_DELTA) -> np.ndarray:
    """bits: (nh,nw,ncoef) uint8 -> new luma with the bits written in."""
    ds = _deltas(delta)
    c = dct2(luma)
    for k, (u, v) in enumerate(COEFFS):
        c[..., u, v] = qim_point(c[..., u, v], bits[..., k], ds[k])
    return idct2(c)


def extract(luma: np.ndarray, delta=DEFAULT_DELTA) -> np.ndarray:
    """-> (nh,nw,ncoef) uint8 hard decisions."""
    ds = _deltas(delta)
    c = dct2(luma)
    out = np.empty(c.shape[:2] + (len(COEFFS),), dtype=np.uint8)
    for k, (u, v) in enumerate(COEFFS):
        q = np.round(2.0 * c[..., u, v] / ds[k]).astype(np.int64)
        out[..., k] = (q & 1).astype(np.uint8)
    return out


def soft(luma: np.ndarray, delta=DEFAULT_DELTA) -> np.ndarray:
    """-> (nh,nw,ncoef) float32 signed distance to the decision boundary.

    Positive means bit 1.  Magnitude is the normalised margin.
    """
    ds = _deltas(delta)
    c = dct2(luma)
    out = np.empty(c.shape[:2] + (len(COEFFS),), dtype=np.float32)
    for k, (u, v) in enumerate(COEFFS):
        r = (2.0 * c[..., u, v] / ds[k]) % 2.0
        out[..., k] = np.where(r > 1.0, r - 2.0, r)
    return out

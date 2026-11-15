# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 revimg contributors

"""Reversible mosaic scramble: keyed tile permutation + per-tile D4 transforms.

The output is an ordinary image whose tiles have been (a) permuted and (b)
individually rotated/flipped.  It opens in any viewer, looks like a shuffled
collage, and is exactly invertible given the key -- no resampling, no colour
changes, so in `lossless` mode the round trip is bit-exact.

Robustness property that matters later: the scramble is a *permutation of whole
tiles*, so a crop or a smudge destroys the tiles it covers and leaves every
other tile byte-identical.  Scrambled tiles are never blended together.
"""

from __future__ import annotations

import numpy as np

TILE = 32


def layout(ny: int, nx: int, seed: int):
    rng = np.random.Generator(np.random.PCG64(int(seed) & 0xFFFFFFFFFFFFFFFF))
    perm = rng.permutation(ny * nx).astype(np.int64)
    d4 = rng.integers(0, 8, size=ny * nx).astype(np.int64)
    return perm, d4


def _d4_batch(a: np.ndarray, t: int) -> np.ndarray:
    """Same transform for a stack of tiles: a is (k, T, T, C)."""
    out = np.rot90(a, int(t) & 3, axes=(1, 2))
    if int(t) & 4:
        out = out[:, :, ::-1]
    return out


def _d4_inv_batch(a: np.ndarray, t: int) -> np.ndarray:
    out = a
    if int(t) & 4:
        out = out[:, :, ::-1]
    return np.rot90(out, -(int(t) & 3), axes=(1, 2))


def _as_flat(img: np.ndarray, ny: int, nx: int, tile: int):
    h, w = img.shape[:2]
    c = img.shape[2] if img.ndim == 3 else 1
    arr = np.asarray(img).reshape(ny, tile, nx, tile, c).transpose(0, 2, 1, 3, 4)
    return arr.reshape(ny * nx, tile, tile, c), c


def scramble(img: np.ndarray, seed: int, tile: int = TILE) -> np.ndarray:
    """Vectorised: transform each source tile, then scatter it to perm[p]."""
    h, w = img.shape[:2]
    ny, nx = h // tile, w // tile
    perm, d4 = layout(ny, nx, seed)
    flat, c = _as_flat(img, ny, nx, tile)
    out = np.empty_like(flat)
    for t in range(8):
        m = d4 == t                      # source indices carrying transform t
        if m.any():
            idx = np.nonzero(m)[0]
            out[perm[idx]] = _d4_batch(flat[idx], t)
    res = out.reshape(ny, nx, tile, tile, c).transpose(0, 2, 1, 3, 4)
    res = res.reshape(ny * tile, nx * tile, c)
    return res if img.ndim == 3 else res[..., 0]


def unscramble(img: np.ndarray, seed: int, tile: int = TILE) -> np.ndarray:
    """Exact inverse of `scramble`: out[p] = d4_inv(img[perm[p]], d4[p])."""
    h, w = img.shape[:2]
    ny, nx = h // tile, w // tile
    perm, d4 = layout(ny, nx, seed)
    flat, c = _as_flat(img, ny, nx, tile)
    gathered = flat[perm]                 # the tile that sits at scrambled slot perm[p]
    out = np.empty_like(flat)
    for t in range(8):
        m = d4 == t
        if m.any():
            idx = np.nonzero(m)[0]
            out[idx] = _d4_inv_batch(gathered[idx], t)
    res = out.reshape(ny, nx, tile, tile, c).transpose(0, 2, 1, 3, 4)
    res = res.reshape(ny * tile, nx * tile, c)
    return res if img.ndim == 3 else res[..., 0]


def seam_stats(img: np.ndarray, tile: int = TILE):
    """(mean gradient on tile borders, mean gradient inside tiles)."""
    g = img.astype(np.float32).mean(axis=2) if img.ndim == 3 else img.astype(np.float32)
    h, w = g.shape
    ny, nx = h // tile, w // tile
    if ny < 2 or nx < 2:
        return 1.0, 1.0
    gx = np.abs(np.diff(g, axis=1))
    gy = np.abs(np.diff(g, axis=0))
    bx = np.zeros(gx.shape[1], dtype=bool)
    bx[tile - 1::tile] = True
    by = np.zeros(gy.shape[0], dtype=bool)
    by[tile - 1::tile] = True
    inner_x = gx[:, ~bx] if (~bx).any() else gx
    border_x = gx[:, bx] if bx.any() else gx
    inner_y = gy[~by] if (~by).any() else gy
    border_y = gy[by] if by.any() else gy
    border = 0.5 * float(border_x.mean()) + 0.5 * float(border_y.mean())
    interior = 0.5 * float(inner_x.mean()) + 0.5 * float(inner_y.mean())
    return border, interior


def seam_score(img: np.ndarray, tile: int = TILE) -> float:
    """Ratio of gradient energy on tile borders vs. inside tiles.

    ~1.0 means the tile grid is invisible (i.e. the image was correctly
    reassembled); a large value means the mosaic is still scrambled or was
    reassembled with the wrong key/geometry.
    """
    border, interior = seam_stats(img, tile)
    return border / (interior + 1e-6)

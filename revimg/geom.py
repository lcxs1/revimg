# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 revimg contributors

"""Geometry: find the carrier's block lattice in a damaged image.

The mosaic carrier is built from `tile`-sized square tiles, so its strongest
periodic signal is the grid of tile seams.  Projecting the horizontal /
vertical gradient onto each axis turns that grid into a 1-D periodic signal
whose period and phase we recover with an FFT.  That single measurement yields

    * the scale       (period in received pixels / nominal tile size)
    * the sub-block phase

and it survives JPEG, cropping and resizing, because seams are low-frequency
square waves.  The remaining ambiguity -- which *page* the lattice starts on --
is 16 x 16 possibilities, resolved later by validating page checksums.
"""

from __future__ import annotations

import numpy as np
from PIL import Image


def _detrend(x: np.ndarray, win: int = 31) -> np.ndarray:
    if len(x) < win + 2:
        return x - x.mean()
    k = np.ones(win, dtype=np.float32) / win
    pad = win // 2
    xp = np.pad(x.astype(np.float32), (pad, pad), mode="edge")
    base = np.convolve(xp, k, mode="valid")[: len(x)]
    return x.astype(np.float32) - base


def _peaks(sig: np.ndarray, lo: float, hi: float, top: int = 3):
    """Dominant periodic components with period in [lo, hi] pixels."""
    n = len(sig)
    if n < 16:
        return []
    s = _detrend(sig)
    s = s * np.hanning(n)
    f = np.fft.rfft(s)
    mag = np.abs(f)
    kmin = max(1, int(np.floor(1.0 / hi * n)))
    kmax = min(len(mag) - 1, int(np.ceil(1.0 / lo * n)))
    if kmax <= kmin:
        return []
    band = mag[kmin : kmax + 1]
    out = []
    order = np.argsort(band)[::-1][: max(top * 3, 6)]
    for oi in order:
        k = kmin + int(oi)
        if k <= 0 or k >= len(mag) - 1:
            continue
        a, b, c = mag[k - 1], mag[k], mag[k + 1]
        denom = a - 2 * b + c
        delta = 0.5 * (a - c) / denom if denom != 0 else 0.0
        delta = float(np.clip(delta, -0.5, 0.5))
        kf = k + delta
        if kf <= 0:
            continue
        period = n / kf
        # x(x) ~ comb at x0 + m*period  =>  DFT phase at the peak is -2*pi*x0/period
        x0 = -float(np.angle(f[k])) * period / (2.0 * np.pi)
        x0 %= period
        x0 = _refine(sig, period, x0)
        strength = float(mag[k] / (band.mean() + 1e-9))
        out.append((period, float(x0), strength))
        if len(out) >= top:
            break
    return out


def _refine(sig: np.ndarray, period: float, phase: float, span: float = 1.5,
            step: float = 0.05) -> float:
    """Pick the sub-pixel phase whose predicted lattice best matches the peaks."""
    n = len(sig)
    base = np.arange(n, dtype=np.float64)
    best_phase, best_score = phase % period, -1e18
    for cand in np.arange(phase - span, phase + span + 1e-9, step):
        xs = np.arange(0.0, n, period) + cand
        xs = xs[(xs >= 0) & (xs < n - 1)]
        if len(xs) < 3:
            continue
        score = float(np.interp(xs, base, sig).mean())
        if score > best_score:
            best_score, best_phase = score, float(cand % period)
    return best_phase


def edge_projections(gray: np.ndarray):
    """Mean absolute gradient per column / per row, indexed by *boundary*.

    `np.diff` puts the step between pixels i and i+1 at index i, so a leading
    zero realigns the array: entry b is the edge at coordinate b.  Without this
    the recovered lattice phase is one pixel off, which silently misaligns
    every 8x8 block.
    """
    g = gray.astype(np.float32)
    if g.shape[1] > 1:
        gx = np.concatenate([[0.0], np.abs(np.diff(g, axis=1)).mean(axis=0)])
    else:
        gx = np.zeros(1)
    if g.shape[0] > 1:
        gy = np.concatenate([[0.0], np.abs(np.diff(g, axis=0)).mean(axis=1)])
    else:
        gy = np.zeros(1)
    return gx, gy


def _fold_score(sig: np.ndarray, p: float, bins: int = 96):
    """Fold the 1-D profile at period `p`; a real lattice gives a sharp peak.

    Returns (sharpness, profile).  Sharpness is the peak height above the mean
    in units of the profile's spread, so it is scale free and immune to the DC
    level of |gradient|.
    """
    n = len(sig)
    if p < 1.5 or n / p < 3.0:
        return -1.0, None
    idx = ((np.arange(n, dtype=np.float64) / p) % 1.0 * bins).astype(np.int64)
    idx = np.minimum(idx, bins - 1)
    tot = np.bincount(idx, weights=sig, minlength=bins)
    cnt = np.bincount(idx, minlength=bins)
    prof = tot / np.maximum(cnt, 1)
    sd = float(prof.std())
    if sd <= 1e-12:
        return -1.0, prof
    return float((prof.max() - prof.mean()) / sd), prof


def _refine_lattice(sig: np.ndarray, p0: float, span: float = 0.04):
    """Local search on period, then read the phase off the folded profile.

    Two passes: the first finds the lattice among nearby ones, the second pins
    the period down to ~0.01%.  That precision matters because the phase is
    extrapolated across the whole picture -- a 0.1% period error puts the far
    side of a 2000-pixel image a quarter of a block off.
    """
    best = (-1.0, p0, None)
    for p in np.linspace(p0 * (1.0 - span), p0 * (1.0 + span), 41):
        s, prof = _fold_score(sig, p)
        if s > best[0]:
            best = (s, p, prof)
    _s, p1, _prof = best
    for p in np.linspace(p1 * 0.996, p1 * 1.004, 41):
        s, prof = _fold_score(sig, p)
        if s > best[0]:
            best = (s, p, prof)
    sharp, p, prof = best
    if prof is None:
        return p, 0.0, sharp
    b = len(prof)
    k = int(np.argmax(prof))
    y0, y1, y2 = prof[(k - 1) % b], prof[k], prof[(k + 1) % b]
    den = y0 - 2.0 * y1 + y2
    d = 0.5 * (y0 - y2) / den if den != 0 else 0.0
    phase = ((k + float(np.clip(d, -0.5, 0.5))) / b) * p
    return float(p), float(phase % p), float(sharp)


def _lattice(sig: np.ndarray, lo: float, hi: float, top: int):
    """FFT picks the candidate periods, folding then nails period *and* phase.

    Folding matters because the FFT phase is only good to a fraction of a bin
    and its period estimate drifts by ~0.05 px over a hundred blocks, which is
    enough to misalign every 8x8 block at the far side of the picture.
    """
    sig = sig - sig.mean()
    peaks = _peaks(sig + sig.mean(), lo, hi, max(top, 2))
    out = []
    for p0, _ph, _s in peaks:
        p, ph, sharp = _refine_lattice(sig, p0)
        if abs(p - p0) > 0.06 * p0:      # folding ran away: keep the FFT answer
            p, ph, sharp = _refine_lattice(sig, p0 * 1.0, span=0.02)
        out.append((float(p), float(ph), float(sharp)))
    return out[:top]


def estimate(gray: np.ndarray, lo: float = 2.5, hi: float = 160.0,
             top: int = 3):
    """Strongest periodicities of the carrier grid, as (period, phase, sharpness).

    The carrier produces *two* combs: the 8-pixel QIM block grid and the 4-block
    tile grid (with harmonics).  Which one dominates depends on the image, so
    callers must treat the returned period as a hypothesis and try its
    sub/super-multiples (see `codec.candidates`).
    """
    gx, gy = edge_projections(gray)
    return _lattice(gx, lo, hi, top), _lattice(gy, lo, hi, top)


def to_canvas(arr: np.ndarray, bpx: float, bpy: float, phix: float, phiy: float,
              block: int = 8, size=None):
    """Resample so that 1 output pixel == 1 nominal (undamaged) pixel.

    `bpx`/`bpy` are the detected *block* periods in received pixels (nominal
    `block` pixels) and `phix`/`phiy` a lattice point.  Output pixel (0,0) sits
    at received pixel (phix, phiy); the absolute lattice index is resolved by
    the page-phase search in the codec.  Returns (canvas_rgb, scale_x, scale_y).
    """
    if bpx <= 0.5 or bpy <= 0.5:
        return None, 1.0, 1.0
    a = block / bpx          # canvas pixels per received pixel
    b = block / bpy
    ix = 1.0 / a             # output->input scale
    iy = 1.0 / b
    h, w = arr.shape[:2]
    if size is not None:
        wc, hc = int(size[0]), int(size[1])
    else:
        wc = int(np.floor((w - phix - 1.0) * a)) + 1
        hc = int(np.floor((h - phiy - 1.0) * b)) + 1
        wc -= wc % 8
        hc -= hc % 8
    if wc < 8 or hc < 8:
        return None, 1.0 / a, 1.0 / b
    im = Image.fromarray(arr)
    out = im.transform(
        (wc, hc), Image.AFFINE, (ix, 0.0, phix, 0.0, iy, phiy),
        resample=Image.BILINEAR,
    )
    return np.asarray(out), 1.0 / a, 1.0 / b


def canvas_size(w: int, h: int, bpx: float, bpy: float, phix: float, phiy: float,
                block: int = 8, tile: int = 32):
    """Estimate the *original* canvas size from the detected lattice.

    Rounding the resampled extent loses up to a tile at each edge, and a single
    tile of error changes the permutation's shape, which would garble a
    mosaic-only recovery.  Deriving the size from the scale instead keeps it
    exact for clean integer ratios.
    """
    cw = int(round((w - phix) * block / bpx / tile)) * tile
    ch = int(round((h - phiy) * block / bpy / tile)) * tile
    return max(tile, cw), max(tile, ch)

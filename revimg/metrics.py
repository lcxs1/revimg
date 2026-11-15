# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 revimg contributors

"""Image quality metrics (numpy only)."""

from __future__ import annotations

import numpy as np


def to_gray(a: np.ndarray) -> np.ndarray:
    a = np.asarray(a, dtype=np.float64)
    if a.ndim == 3:
        return a[..., 0] * 0.299 + a[..., 1] * 0.587 + a[..., 2] * 0.114
    return a


def mse(a: np.ndarray, b: np.ndarray) -> float:
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    if a.shape != b.shape:
        raise ValueError(f"shape mismatch {a.shape} vs {b.shape}")
    return float(np.mean((a - b) ** 2))


def psnr(a: np.ndarray, b: np.ndarray) -> float:
    m = mse(a, b)
    if m <= 1e-12:
        return 99.0
    return float(10.0 * np.log10(255.0 * 255.0 / m))


def ssim(a: np.ndarray, b: np.ndarray, block: int = 8) -> float:
    """Mean SSIM over non-overlapping `block` x `block` windows (grayscale)."""
    x = to_gray(a)
    y = to_gray(b)
    if x.shape != y.shape:
        raise ValueError(f"shape mismatch {x.shape} vs {y.shape}")
    h, w = x.shape
    bh, bw = h // block, w // block
    if bh == 0 or bw == 0:
        return 1.0 if np.allclose(x, y) else 0.0
    x = x[: bh * block, : bw * block].reshape(bh, block, bw, block).transpose(0, 2, 1, 3)
    y = y[: bh * block, : bw * block].reshape(bh, block, bw, block).transpose(0, 2, 1, 3)
    x = x.reshape(-1, block * block)
    y = y.reshape(-1, block * block)
    c1 = (0.01 * 255) ** 2
    c2 = (0.03 * 255) ** 2
    mx = x.mean(axis=1)
    my = y.mean(axis=1)
    vx = x.var(axis=1)
    vy = y.var(axis=1)
    cxy = ((x - mx[:, None]) * (y - my[:, None])).mean(axis=1)
    num = (2 * mx * my + c1) * (2 * cxy + c2)
    den = (mx * mx + my * my + c1) * (vx + vy + c2)
    return float(np.mean(num / np.maximum(den, 1e-12)))


def summary(a: np.ndarray, b: np.ndarray) -> dict:
    return {"psnr": round(psnr(a, b), 2), "ssim": round(ssim(a, b), 4)}

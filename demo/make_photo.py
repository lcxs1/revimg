# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 revimg contributors

"""Build a believable test photograph without any external asset."""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw, ImageFilter


def make_photo(w=1024, h=1024, seed=7) -> Image.Image:
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    u = xx / (w - 1)
    v = yy / (h - 1)

    # sky / ground gradient with a vignette and a bit of film grain
    sky = np.stack([
        40 + 150 * v ** 1.4,
        90 + 120 * v ** 1.1,
        150 + 90 * v ** 0.6,
    ], axis=-1)
    ground = np.stack([
        20 + 170 * u,
        90 + 60 * (1 - u),
        30 + 40 * np.sin(6 * u),
    ], axis=-1)
    mask = (v > 0.62 + 0.06 * np.sin(5 * u)).astype(np.float32)[..., None]
    arr = sky * (1 - mask) + ground * mask

    # smooth large-scale luminance variation
    for _ in range(6):
        cy, cx = rng.uniform(0, h), rng.uniform(0, w)
        s = rng.uniform(w * 0.1, w * 0.4)
        g = np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * s * s)))
        arr += (rng.uniform(-35, 35) * g)[..., None] * np.array([1.0, 0.8, 0.6])

    arr = np.clip(arr, 0, 255)
    im = Image.fromarray(arr.astype(np.uint8))

    d = ImageDraw.Draw(im)
    # geometric structure: gives edges, texture and fine detail to lose
    for i in range(40):
        x0, y0 = rng.integers(0, w), rng.integers(0, h)
        r = rng.integers(20, 190)
        col = tuple(int(c) for c in rng.integers(0, 256, 3))
        if i % 3 == 0:
            d.rectangle([x0, y0, x0 + r, y0 + r // 2], outline=col, width=3)
        elif i % 3 == 1:
            d.ellipse([x0 - r, y0 - r // 2, x0 + r, y0 + r // 2], fill=col)
        else:
            d.line([x0, y0, x0 + r, y0 - r // 2], fill=col, width=4)
    for i in range(24):
        x0, y0 = rng.integers(0, w), rng.integers(0, h)
        d.text((x0, y0), "REVIMG %d" % i, fill=(250, 250, 250))

    # texture: thousands of tiny dots = the fine detail a coarse layer cannot hold
    px = np.asarray(im).copy()
    for _ in range(4000):
        x, y = rng.integers(0, w), rng.integers(0, h)
        px[y, x] = rng.integers(0, 256, 3)
    im = Image.fromarray(px).filter(ImageFilter.GaussianBlur(0.4))

    arr = np.asarray(im).astype(np.float32) + rng.normal(0, 3.0, (h, w, 3))
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


if __name__ == "__main__":
    import pathlib
    import sys

    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "dev/sample.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    make_photo().save(out)
    print("wrote", out)

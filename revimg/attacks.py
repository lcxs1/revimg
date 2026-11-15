# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 revimg contributors

"""Attacks a picture actually suffers online, as reproducible functions.

Every attack takes and returns a PIL image; the decoder is expected to work on
the attacked file only, with no knowledge of the attack.
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont


def jpeg(im: Image.Image, quality: int = 25) -> Image.Image:
    b = io.BytesIO()
    im.convert("RGB").save(b, format="JPEG", quality=int(quality), optimize=True)
    b.seek(0)
    with Image.open(b) as o:
        o.load()
        return o.convert("RGB")


def crop(im: Image.Image, frac: float = 0.5, where: str = "center") -> Image.Image:
    """Keep `frac` of the area (linear fraction of each side is sqrt-ish safe)."""
    w, h = im.size
    keep = float(np.sqrt(max(frac, 1e-6)))
    cw, ch = max(32, int(round(w * keep))), max(32, int(round(h * keep)))
    if where == "center":
        x0, y0 = (w - cw) // 2, (h - ch) // 2
    elif where == "tl":
        x0, y0 = 0, 0
    elif where == "br":
        x0, y0 = w - cw, h - ch
    elif where == "band":            # keep a horizontal band through the middle
        cw = w
        x0 = 0
        y0 = (h - ch) // 2
    else:
        rng = np.random.default_rng(hash(where) & 0xFFFF)
        x0 = int(rng.integers(0, max(1, w - cw)))
        y0 = int(rng.integers(0, max(1, h - ch)))
    return im.crop((x0, y0, x0 + cw, y0 + ch))


def scribble(im: Image.Image, n: int = 10, width: float = 0.06, seed: int = 1,
             color=(230, 30, 30)) -> Image.Image:
    """Heavy marker strokes across the picture (graffiti over a photo)."""
    w, h = im.size
    rng = np.random.default_rng(seed)
    out = im.copy()
    d = ImageDraw.Draw(out)
    wd = max(3, int(round(min(w, h) * width)))
    for _ in range(n):
        pts = [(int(rng.integers(0, w)), int(rng.integers(0, h))) for _ in range(4)]
        col = tuple(int(c) for c in color) if rng.random() < 0.7 else tuple(
            int(c) for c in rng.integers(0, 256, 3))
        d.line(pts, fill=col, width=wd, joint="curve")
    return out


def blobs(im: Image.Image, n: int = 6, radius: float = 0.09, seed: int = 2) -> Image.Image:
    """Filled shapes: the 'someone painted over it' case."""
    w, h = im.size
    rng = np.random.default_rng(seed)
    out = im.copy()
    d = ImageDraw.Draw(out)
    r = max(4, int(min(w, h) * radius))
    for _ in range(n):
        x, y = int(rng.integers(0, w)), int(rng.integers(0, h))
        col = tuple(int(c) for c in rng.integers(0, 256, 3))
        if rng.random() < 0.5:
            d.ellipse([x - r, y - r, x + r, y + r], fill=col)
        else:
            d.rectangle([x - r, y - r // 2, x + r, y + r // 2], fill=col)
    return out


def _font(size: int):
    for name in ("arialbd.ttf", "arial.ttf", "segoeuib.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def watermark(im: Image.Image, text: str = "SAMPLE", alpha: int = 120,
              scale: float = 0.10, repeat: int = 3) -> Image.Image:
    """Semi-transparent text stamped across the picture, repeated diagonally."""
    w, h = im.size
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    f = _font(max(12, int(min(w, h) * scale)))
    try:
        tw = d.textlength(text, font=f)
    except Exception:
        tw = len(text) * scale * min(w, h) * 0.6
    step_x = int(tw * 1.6) + 20
    step_y = int(min(w, h) * scale * 2.2) + 20
    for j in range(-1, repeat + 1):
        for i in range(-1, repeat + 1):
            d.text((i * step_x, j * step_y), text, font=f, fill=(255, 255, 255, alpha))
    layer = layer.rotate(0)
    base = im.convert("RGBA")
    return Image.alpha_composite(base, layer).convert("RGB")


def resize(im: Image.Image, scale: float = 0.5) -> Image.Image:
    w, h = im.size
    nw, nh = max(8, int(round(w * scale))), max(8, int(round(h * scale)))
    return im.resize((nw, nh), Image.BICUBIC)


ALL = {
    "jpeg50": lambda im: jpeg(im, 50),
    "jpeg25": lambda im: jpeg(im, 25),
    "jpeg12": lambda im: jpeg(im, 12),
    # "save as JPEG and send it" -- the everyday path, at the qualities photo
    # sites and chat apps actually use.
    "save_q95": lambda im: jpeg(im, 95),
    "save_q85": lambda im: jpeg(im, 85),
    "save_q75": lambda im: jpeg(im, 75),
    "crop50": lambda im: crop(im, 0.50),
    "crop25": lambda im: crop(im, 0.25),
    "crop10": lambda im: crop(im, 0.10),
    "crop_tl50": lambda im: crop(im, 0.50, "tl"),
    "scribble": lambda im: scribble(im, n=10, width=0.05),
    "graffiti": lambda im: scribble(im, n=22, width=0.09, seed=5),
    "blobs": lambda im: blobs(im, n=8, radius=0.10),
    "watermark": lambda im: watermark(im),
    "resize50": lambda im: resize(im, 0.5),
    "resize35": lambda im: resize(im, 0.35),
    "resize200": lambda im: resize(im, 2.0),
    "mix_hard": lambda im: jpeg(scribble(resize(im, 0.6), n=8, width=0.05), 30),
}

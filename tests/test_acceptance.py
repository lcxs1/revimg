# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 revimg contributors

"""Acceptance gate: the damage tolerance this tool promises.

Run against the *smallest supported image* (2000x2000) with the attack baseline
agreed for the project:

    JPEG q25, centre/ corner crop 50%, 40%-area scribble, 0.5x and 2x rescale,
    a straight "save as JPEG and send it" at q85,

plus the fallback floor: the redundancy thumbnail must stay at least 128 px and
the thumbnail-only recovery must stay at or above 28 dB.

Thresholds sit below the measured values so the gate fires on a real regression
and not on run-to-run noise.  If you change the codec, run this file: it is the
contract, everything else is a detail.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from revimg import attacks, codec, metrics  # noqa: E402
from revimg.bench import (ACCEPTANCE, MIN_THUMB_PSNR, MIN_THUMB_SIDE,  # noqa: E402
                          MIN_THUMB_SSIM)

SIZE = 2000
PASSWORD = "acceptance-baseline"


def photo(w=SIZE, h=SIZE, seed=33):
    """Deterministic 2000x2000 stand-in for a typical photograph.

    Deliberately *not* a noise field: a real photo is mostly smooth regions with
    edges and some fine texture.  PSNR is content-dependent -- a thumbnail of a
    bush scores far worse than a thumbnail of a sky at identical visual quality
    -- so the acceptance image has to look like a photo or the gate measures the
    wrong thing.
    """
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    u, v = xx / (w - 1), yy / (h - 1)
    sky = np.stack([45 + 140 * v ** 1.4, 95 + 110 * v, 155 + 80 * v ** 0.6], axis=-1)
    ground = np.stack([25 + 165 * u, 85 + 65 * (1 - u), 30 + 40 * np.sin(6 * u)], axis=-1)
    mask = (v > 0.62 + 0.05 * np.sin(5 * u)).astype(np.float32)[..., None]
    arr = sky * (1 - mask) + ground * mask
    for _ in range(8):
        cy, cx = rng.uniform(0, h), rng.uniform(0, w)
        s = rng.uniform(w * 0.08, w * 0.35)
        g = np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * s * s)))
        arr += (rng.uniform(-30, 30) * g)[..., None] * np.array([1.0, 0.8, 0.6])
    im = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(im)
    for i in range(50):
        x0, y0 = int(rng.integers(0, w)), int(rng.integers(0, h))
        r = int(rng.integers(30, 200))
        col = tuple(int(c) for c in rng.integers(0, 256, 3))
        if i % 3 == 0:
            d.rectangle([x0, y0, x0 + r, y0 + r // 2], outline=col, width=3)
        elif i % 3 == 1:
            d.ellipse([x0 - r, y0 - r // 2, x0 + r, y0 + r // 2], fill=col)
        else:
            d.line([x0, y0, x0 + r, y0 - r // 2], fill=col, width=4)
    px = np.asarray(im).copy()
    for _ in range(6000):                      # fine detail a thumbnail cannot hold
        px[int(rng.integers(0, h)), int(rng.integers(0, w))] = rng.integers(0, 256, 3)
    arr = np.asarray(Image.fromarray(px)).astype(np.float32) + rng.normal(0, 3.0, (h, w, 3))
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


class TestAcceptance(unittest.TestCase):
    """One encode, then every attack the project promises to survive."""

    @classmethod
    def setUpClass(cls):
        cls.src = photo()
        cls.A = np.asarray(cls.src)
        cls.carrier, cls.rep = codec.encode(cls.src, PASSWORD)

    def _gate(self, name, attacked, min_psnr=None, min_ssim=None):
        min_psnr = ACCEPTANCE.get(name, (0.0, 0.0))[0] if min_psnr is None else min_psnr
        min_ssim = ACCEPTANCE.get(name, (0.0, 0.0))[1] if min_ssim is None else min_ssim
        out, rep = codec.decode(attacked, PASSWORD)
        self.assertIsNotNone(out, f"{name}: nothing recovered ({rep.reason})")
        B = np.asarray(out)
        if B.shape != self.A.shape:
            # The mosaic-only fallback has no payload, so it cannot know the
            # original size and returns the padded canvas.  Compare at the
            # original resolution, as the benchmark does.
            B = np.asarray(Image.fromarray(B).resize(
                (self.A.shape[1], self.A.shape[0]), Image.LANCZOS))
        m = metrics.summary(self.A, B)
        self.assertGreaterEqual(m["psnr"], min_psnr, f"{name}: {m}")
        self.assertGreaterEqual(m["ssim"], min_ssim, f"{name}: {m}")
        return rep

    # -- the agreed baseline ------------------------------------------------
    def test_jpeg_q25(self):
        self._gate("jpeg25", attacks.jpeg(self.carrier, 25))

    def test_saved_as_jpeg_q85(self):
        self._gate("save_q85", attacks.jpeg(self.carrier, 85))

    def test_crop_center_50(self):
        rep = self._gate("crop50", attacks.crop(self.carrier, 0.5))
        self.assertGreater(rep.coverage, 0.35, "crop left too little of the canvas")

    def test_crop_corner_50(self):
        self._gate("crop_tl50", attacks.crop(self.carrier, 0.5, "tl"))

    def test_scribble_40_percent(self):
        self._gate("scribble", attacks.scribble(self.carrier, n=10, width=0.05))

    def test_resize_half(self):
        self._gate("resize50", attacks.resize(self.carrier, 0.5))

    def test_resize_double(self):
        self._gate("resize200", attacks.resize(self.carrier, 2.0))

    # -- the fallback floor ------------------------------------------------
    def test_thumbnail_floor(self):
        side = min(self.rep.layers[0]["w"], self.rep.layers[0]["h"])
        self.assertGreaterEqual(side, MIN_THUMB_SIDE,
                                f"redundancy thumbnail is only {side}px")
        out, _rep = codec.decode(self.carrier, PASSWORD, force="coarse")
        self.assertIsNotNone(out)
        B = np.asarray(out)
        m = metrics.summary(self.A, B)
        self.assertGreaterEqual(m["psnr"], MIN_THUMB_PSNR, f"thumbnail-only: {m}")
        self.assertGreaterEqual(m["ssim"], MIN_THUMB_SSIM, f"thumbnail-only: {m}")

    def test_wrong_password_refuses(self):
        out, rep = codec.decode(self.carrier, "not-the-password")
        self.assertIsNone(out)
        self.assertFalse(rep.ok)

    def test_lossless_is_exact(self):
        carrier, _ = codec.encode(self.src, PASSWORD, lossless=True)
        out, _ = codec.decode(carrier, PASSWORD)
        self.assertIsNotNone(out)
        B = np.asarray(out)[: self.A.shape[0], : self.A.shape[1]]
        self.assertEqual(B.shape, self.A.shape)
        self.assertTrue((B == self.A).all(), "lossless round trip must be exact")


if __name__ == "__main__":
    unittest.main(verbosity=2)

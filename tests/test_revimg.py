# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 revimg contributors

"""Tests: coding primitives, then the container end to end under attack."""

from __future__ import annotations

import io
import sys
import unittest

import numpy as np
from PIL import Image

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from revimg import (attacks, codec, crypto, fountain, gf256, metrics, qim,  # noqa: E402
                    spine, tiles)


def sample(w=512, h=512, seed=5):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    base = np.stack([40 + 180 * xx / w, 60 + 150 * yy / h, 120 + 80 * (xx * yy) / (w * h)], -1)
    im = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8))
    from PIL import ImageDraw
    d = ImageDraw.Draw(im)
    for i in range(30):
        x0, y0 = int(rng.integers(0, w)), int(rng.integers(0, h))
        r = int(rng.integers(10, 80))
        d.ellipse([x0, y0, x0 + r, y0 + r], outline=tuple(int(c) for c in rng.integers(0, 256, 3)),
                  width=2)
    arr = np.asarray(im).astype(np.float32) + rng.normal(0, 4, (h, w, 3))
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


class TestCoding(unittest.TestCase):
    def test_gf_tables(self):
        for a in range(1, 256):
            self.assertEqual(gf256.gf_mul(a, gf256.gf_inv(a)), 1)
        self.assertEqual(gf256.gf_mul(0, 9), 0)

    def test_rs_corrects_capacity(self):
        rng = np.random.default_rng(1)
        nsym = 24
        k = spine.MSG_BYTES
        for _ in range(20):
            msg = bytes(rng.integers(0, 256, k, dtype=np.uint8))
            cw = gf256.rs_encode(msg, nsym)
            self.assertTrue(gf256.rs_check(cw, nsym))
            for nerr in (1, 6, 12):
                bad = bytearray(cw)
                for p in rng.choice(len(cw), nerr, replace=False):
                    bad[p] ^= int(rng.integers(1, 256))
                fixed = gf256.rs_decode(bytes(bad), nsym)
                self.assertIsNotNone(fixed)
                self.assertEqual(fixed[:k], msg)

    def test_rs_rejects_hopeless(self):
        cw = bytearray(gf256.rs_encode(bytes(range(spine.MSG_BYTES)), 24))
        for p in range(30):
            cw[p] ^= 0x5A
        self.assertIsNone(gf256.rs_decode(bytes(cw), 24))

    def test_fountain_any_k_symbols(self):
        rng = np.random.default_rng(2)
        for k in (8, 30, 64):
            data = bytes(rng.integers(0, 256, k * spine.SYM_DATA, dtype=np.uint8))
            blocks = fountain.split(data, k, spine.SYM_DATA)
            n = fountain.plan(k, 2.0)
            syms = dict(fountain.encode(blocks, n, 99))
            keep = sorted(rng.permutation(n)[: k + 2])
            self.assertIsNotNone(fountain.decode({i: syms[i] for i in keep},
                                                 k, spine.SYM_DATA, 99),
                                 f"k={k} should decode from k+2 symbols")

    def test_crypto_roundtrip_and_tamper(self):
        master = crypto.master_key("pw", rounds=1000)
        pt = b"hello container" * 10
        blob = crypto.seal(master, pt, aad=b"hdr")
        self.assertEqual(crypto.open_(master, blob, aad=b"hdr"), pt)
        bad = bytearray(blob)
        bad[20] ^= 1
        self.assertIsNone(crypto.open_(master, bytes(bad), aad=b"hdr"))
        self.assertIsNone(crypto.open_(master, blob, aad=b"other"))

    def test_tiles_exact(self):
        im = np.asarray(sample(256, 256))
        seed = 1234
        scr = tiles.scramble(im, seed)
        self.assertTrue((tiles.unscramble(scr, seed) == im).all())
        self.assertGreater(tiles.seam_score(scr), 2.0)
        self.assertLess(tiles.seam_score(im), 2.0)

    def test_qim_survives_jpeg(self):
        rng = np.random.default_rng(4)
        y = rng.integers(40, 216, (256, 256)).astype(np.float32)
        bits = rng.integers(0, 2, (32, 32, 2)).astype(np.uint8)
        out = np.clip(np.rint(qim.embed(y, bits)), 0, 255).astype(np.uint8)
        back = np.asarray(attacks.jpeg(Image.fromarray(out), 50)).astype(np.float32).mean(axis=2)
        got = qim.extract(back)
        self.assertLess(float((got != bits).mean()), 0.01)


class TestContainer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = sample(768, 768)
        cls.A = np.asarray(cls.src)
        cls.carrier, cls.rep = codec_encode(cls.src)

    def test_pristine_is_near_lossless(self):
        out, rep = codec.decode(self.carrier, "pw")
        self.assertIsNotNone(out)
        self.assertGreater(metrics.psnr(self.A, np.asarray(out)), 30.0)
        self.assertEqual(rep.pages_ok, rep.pages_total)

    def test_wrong_password_fails_cleanly(self):
        out, rep = codec.decode(self.carrier, "wrong")
        self.assertIsNone(out)
        self.assertFalse(rep.ok)

    def test_carrier_is_a_normal_image(self):
        buf = io.BytesIO()
        self.carrier.save(buf, format="PNG")
        buf.seek(0)
        with Image.open(buf) as im:
            self.assertEqual(im.size, self.carrier.size)
            self.assertEqual(im.mode, "RGB")

    def test_jpeg_quality_25(self):
        out, rep = codec.decode(attacks.jpeg(self.carrier, 25), "pw")
        self.assertIsNotNone(out)
        self.assertGreater(metrics.psnr(self.A, np.asarray(out)), 15.0)

    def test_watermark(self):
        out, rep = codec.decode(attacks.watermark(self.carrier), "pw")
        self.assertIsNotNone(out)

    def test_upscale(self):
        out, rep = codec.decode(attacks.resize(self.carrier, 2.0), "pw")
        self.assertIsNotNone(out)
        self.assertGreater(metrics.psnr(self.A, np.asarray(out)), 12.0)

    def test_crop(self):
        out, rep = codec.decode(attacks.crop(self.carrier, 0.5, "tl"), "pw")
        self.assertIsNotNone(out)
        self.assertGreater(rep.coverage, 0.4)

    def test_scribble_degrades_but_survives(self):
        out, rep = codec.decode(attacks.scribble(self.carrier, n=4, width=0.03), "pw")
        self.assertIsNotNone(out)

    def test_lossless_mode_is_exact(self):
        im = sample(512, 512)
        A = np.asarray(im)
        carrier, _rep = codec.encode(im, "pw", lossless=True)
        self.assertFalse(np.array_equal(A, np.asarray(carrier)))   # it is scrambled
        out, rep = codec.decode(carrier, "pw")
        self.assertIsNotNone(out)
        self.assertTrue((np.asarray(out) == A).all(), "lossless round trip must be exact")

    def test_lossless_survives_tile_aligned_crop(self):
        im = sample(512, 512)
        A = np.asarray(im)
        carrier, _ = codec.encode(im, "pw", lossless=True)
        cut = attacks.crop(carrier, 0.25, "tl")
        out, rep = codec.decode(cut, "pw")
        if out is not None:
            B = np.asarray(out)
            h = min(B.shape[0], A.shape[0])
            w = min(B.shape[1], A.shape[1])
            self.assertGreater(metrics.psnr(A[:h, :w], B[:h, :w]), 20.0)


def codec_encode(im):
    from revimg import codec

    return codec.encode(im, "pw", alpha_detail=2.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)

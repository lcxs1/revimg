# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 revimg contributors

"""Build a visual before/after sheet: original, carrier, and recoveries.

    python demo/make_demo.py        # writes demo/comparison.png
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from PIL import Image, ImageDraw, ImageFont
import numpy as np
from revimg import attacks, codec, metrics
from make_photo import make_photo

ROOT = pathlib.Path(__file__).resolve().parent.parent
src = make_photo(2048, 2048, seed=21)
A = np.asarray(src)
carrier, rep = codec.encode(src, "correct horse")
print(rep.as_text())

CASES = [
    ("JPEG q25", attacks.jpeg(carrier, 25)),
    ("crop 50%", attacks.crop(carrier, 0.5)),
    ("crop 50% (corner)", attacks.crop(carrier, 0.5, "tl")),
    ("scribble", attacks.scribble(carrier, n=8, width=0.05)),
    ("watermark", attacks.watermark(carrier)),
    ("resize 0.5x", attacks.resize(carrier, 0.5)),
    ("resize 2x", attacks.resize(carrier, 2.0)),
]

th = 300
rows = []
for name, atk in CASES:
    out, r = codec.decode(atk, "correct horse")
    if out is None:
        rows.append((name, atk, None, r, 0.0, 0.0))
        continue
    B = np.asarray(out.resize(A.shape[1::-1], Image.LANCZOS)) if out.size != src.size else np.asarray(out)
    m = metrics.summary(A, B)
    rows.append((name, atk, out, r, m["psnr"], m["ssim"]))
    print(f"{name:<20} {r.mode:<12} pages {r.pages_ok}/{r.pages_total}  "
          f"PSNR {m['psnr']:.1f}  SSIM {m['ssim']:.3f}")

f = ImageFont.load_default()
titles = ["original", "encrypted carrier"] + [c[0] for c in rows]
ncol = len(titles)
sheet = Image.new("RGB", (th * ncol, th + 26), (18, 18, 22))
d = ImageDraw.Draw(sheet)

def put(i, im):
    im = im.convert("RGB")
    if im.size != (th, th):
        im = im.resize((th, th), Image.LANCZOS)
    sheet.paste(im, (i * th, 26))

put(0, src)
put(1, carrier)
for j, (name, atk, out, r, ps, ss) in enumerate(rows):
    put(2 + j, out if out is not None else atk)
for i, t in enumerate(titles):
    d.text((i * th + 6, 8), t, fill=(235, 235, 235), font=f)
sheet.save(ROOT / "demo" / "comparison.png")
print("wrote", ROOT / "demo" / "comparison.png")

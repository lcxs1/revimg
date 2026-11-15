# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 revimg contributors

"""Benchmark: encode once, attack many ways, measure what survives.

    python -m revimg.bench --image dev/sample.png --password test
"""

from __future__ import annotations

import argparse
import json
import pathlib
import time

import numpy as np
from PIL import Image

from . import attacks, codec, metrics

#: Agreed acceptance baseline, set from measurements on a 2000x2000 carrier with
#: a margin so the gate fires on a real regression rather than on noise.
ACCEPTANCE = {
    "jpeg25":    (26.0, 0.78),
    "save_q85":  (26.0, 0.78),
    "crop50":    (25.0, 0.80),
    "crop_tl50": (25.0, 0.80),
    "scribble":  (23.0, 0.80),
    "resize50":  (17.5, 0.68),
    "resize200": (30.0, 0.80),
}
#: Floor for the redundancy thumbnail.  The *side* is content-independent; the
#: PSNR of any thumbnail is not (a bush costs ~6 dB versus a sky at identical
#: visual quality), so this floor is deliberately loose and SSIM is checked in
#: the acceptance test as the real "is it recognisable" signal.
MIN_THUMB_SIDE = 208
MIN_THUMB_PSNR = 24.0
MIN_THUMB_SSIM = 0.85


def verdict(rows, erep) -> tuple[bool, list]:
    """Compare a benchmark run against ACCEPTANCE.  Returns (ok, lines)."""
    lines = []
    ok_all = True
    for name, (mp, ms) in ACCEPTANCE.items():
        row = next((r for r in rows if r["attack"] == name), None)
        if row is None:
            lines.append(f"  {name:<12} MISSING")
            ok_all = False
            continue
        ok = bool(row["ok"]) and row["psnr"] >= mp and row["ssim"] >= ms
        ok_all &= ok
        lines.append(
            f"  {name:<12} {'PASS' if ok else 'FAIL':<5} "
            f"psnr {row['psnr']:>6.2f} (>= {mp})  ssim {row['ssim']:.3f} (>= {ms})"
        )
    side = min(erep.layers[0]["w"], erep.layers[0]["h"]) if erep.layers else 0
    thumb_ok = side >= MIN_THUMB_SIDE
    ok_all &= thumb_ok
    lines.append(f"  {'thumbnail':<12} {'PASS' if thumb_ok else 'FAIL':<5} "
                 f"{side}px (>= {MIN_THUMB_SIDE})")
    return ok_all, lines


def run(image_path: str, password: str, outdir: str | None = None,
        names: list[str] | None = None, alpha: float = 1.7):
    with Image.open(image_path) as im:
        im.load()
        src = im.convert("RGB")
    A = np.asarray(src)

    t0 = time.time()
    carrier, erep = codec.encode(src, password, alpha_detail=alpha)
    enc_s = time.time() - t0

    rows = []
    tests = list(attacks.ALL.items())
    if names:
        tests = [(k, v) for k, v in tests if k in names]

    rows.append(_row("(none)", A, A, None, carrier, password, 0.0))
    for name, fn in tests:
        t0 = time.time()
        atk = fn(carrier)
        t_attack = time.time() - t0
        r = _row(name, A, A, atk, carrier, password, t_attack)
        rows.append(r)
        if outdir:
            d = pathlib.Path(outdir)
            d.mkdir(parents=True, exist_ok=True)
            atk.save(d / f"attack_{name}.png")
            if isinstance(r.get("image"), Image.Image):
                r["image"].save(d / f"restored_{name}.png")
                del r["image"], r["coarse"]

    return rows, erep, enc_s


def _row(name, A, _A2, atk, carrier, password, t_attack):
    if atk is None:
        return {"attack": name, "ok": True, "psnr": 99.0, "ssim": 1.0, "mode": "-",
                "pages": f"-", "decode_s": 0.0, "detail": ""}
    t0 = time.time()
    out, rep = codec.decode(atk, password)
    dt = time.time() - t0
    if out is None:
        return {"attack": name, "ok": False, "psnr": 0.0, "ssim": 0.0, "mode": "fail",
                "pages": "0", "decode_s": round(dt, 2), "detail": rep.reason,
                "image": None, "coarse": None}
    B = np.asarray(out)
    if B.shape != A.shape:
        B = np.asarray(Image.fromarray(B).resize(A.shape[1::-1], Image.LANCZOS))
    m = metrics.summary(A, B)
    return {
        "attack": name, "ok": True, "psnr": m["psnr"], "ssim": m["ssim"],
        "mode": rep.mode,
        "pages": f"{rep.pages_ok}/{rep.pages_total}",
        "decode_s": round(dt, 2), "detail": f"cover={rep.coverage:.0%}",
        "image": out, "coarse": getattr(out, "revimg_coarse", None),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="measure what survives each attack")
    ap.add_argument("--image", required=True)
    ap.add_argument("--password", default="test")
    ap.add_argument("--alpha", type=float, default=3.2)
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--only", default=None, help="comma separated attack names")
    ap.add_argument("--json", default=None)
    ap.add_argument("--acceptance", action="store_true",
                    help="judge against the agreed baseline and exit non-zero on failure")
    a = ap.parse_args(argv)

    names = a.only.split(",") if a.only else None
    rows, erep, enc_s = run(a.image, a.password, a.outdir, names, a.alpha)

    print(erep.as_text())
    print(f"encode time    : {enc_s:.2f}s\n")
    hdr = f"{'attack':<12}{'result':<8}{'PSNR':>7}{'SSIM':>8}{'mode':>13}{'pages':>10}{'sec':>6}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        res = "ok" if r["ok"] else "FAIL"
        print(f"{r['attack']:<12}{res:<8}{r['psnr']:>7.2f}{r['ssim']:>8.3f}"
              f"{r['mode']:>13}{r['pages']:>10}{r['decode_s']:>6.2f}")
    if a.json:
        clean = [{k: v for k, v in r.items() if k not in ("image", "coarse")} for r in rows]
        pathlib.Path(a.json).write_text(json.dumps(clean, indent=2), encoding="utf-8")
    if a.acceptance:
        ok, lines = verdict(rows, erep)
        print("\nacceptance baseline")
        print("\n".join(lines))
        print(f"\n{'ACCEPTED' if ok else 'REJECTED'}")
        return 0 if ok else 1
    return rows


if __name__ == "__main__":
    raise SystemExit(main())

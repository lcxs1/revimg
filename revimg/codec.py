# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 revimg contributors

"""End-to-end encode / decode.

Container layout
----------------
    visible carrier : keyed tile permutation + per-tile D4 transforms
                      (an ordinary image; opens anywhere; exactly invertible)
    robust payload  : up to two priority layers of thumbnails, each AEAD-sealed,
                      fountain-coded, framed as RS-protected pages and written
                      into the carrier's 8x8 DCT blocks with QIM

The detail layer is what survives small damage; the priority layer is small and
replicated so it still arrives after most of the picture has been destroyed.
Both ride on every part of the canvas, so damage is *erasure*, never
all-or-nothing, and the number of survivors decides the recovered resolution.
"""

from __future__ import annotations

import io
import os
import struct
import time
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

from . import crypto, fountain, geom, qim, spine, tiles

TILE = tiles.TILE
PAD = spine.PAGE_PX          # canvases are padded to a whole page (128 px)
BS = spine.SYM_DATA
#: Public header, readable before decryption:
#:   magic, version | canvas w, h | original w, h | tile | qim quality |
#:   sealed-length | binding nonce | crc16
PUB = struct.Struct(">4sBHHHH" + "BB" + "H" + "8sH")
PUB_LEN = PUB.size
MAGIC = b"RVI1"

LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float32)


# --------------------------------------------------------------------- utils

def _pad_to(arr: np.ndarray, ty: int, tx: int) -> np.ndarray:
    h, w = arr.shape[:2]
    ph = int(np.ceil(h / ty) * ty)
    pw = int(np.ceil(w / tx) * tx)
    if (ph, pw) == (h, w):
        return arr
    out = np.empty((ph, pw) + arr.shape[2:], dtype=arr.dtype)
    out[:h, :w] = arr
    out[h:, :w] = arr[h - 1: h, :w]
    out[:h, w:] = arr[:h, w - 1: w]
    out[h:, w:] = arr[h - 1: h, w - 1: w]
    return out


def _luma(rgb: np.ndarray) -> np.ndarray:
    return (rgb.astype(np.float32) @ LUMA).astype(np.float32)


def _jpeg(im: Image.Image, q: int) -> bytes:
    b = io.BytesIO()
    im.save(b, format="JPEG", quality=int(q), optimize=True, subsampling=2)
    return b.getvalue()


def _resize_max(im: Image.Image, side: int) -> Image.Image:
    w, h = im.size
    s = min(1.0, side / max(w, h))
    nw, nh = max(1, int(round(w * s))), max(1, int(round(h * s)))
    if (nw, nh) == (w, h):
        return im.convert("RGB")
    return im.resize((nw, nh), Image.LANCZOS)


def _upscale(im: Image.Image, size) -> Image.Image:
    return im.resize(size, Image.LANCZOS)


_SIDES = (240, 208, 176, 144, 128, 112, 96, 80, 64, 56, 48, 40, 32, 26, 20, 16, 12)
_QS = (85, 72, 60, 48, 38, 30)


def _thumb_iter(im: Image.Image):
    """Yield (record, thumbnail) from best quality to worst.

    The record is `w, h, kind` followed by either raw RGB or JPEG bytes, so a
    tiny thumbnail can dodge JPEG's fixed table overhead entirely.
    """
    for side in _SIDES:
        th = _resize_max(im, side)
        w, h = th.size
        yield bytes([w, h, 1]) + np.asarray(th, np.uint8).tobytes(), th
        for q in _QS:
            yield bytes([w, h, 0]) + _jpeg(th, q), th


def _pick_thumb(im: Image.Image, max_bytes: int):
    last = None
    for blob, th in _thumb_iter(im):
        last = (blob, th)
        if len(blob) <= max_bytes:
            return blob, th
    return last


def unpack_thumb(blob: bytes) -> Image.Image:
    w, h, kind = blob[0], blob[1], blob[2]
    body = blob[3:]
    if kind == 1:
        return Image.frombytes("RGB", (w, h), body[: w * h * 3])
    im = Image.open(io.BytesIO(body))
    im.load()
    return im.convert("RGB")


def capacity(canvas_w: int, canvas_h: int) -> dict:
    """Page budget for an image of this size, after page padding."""
    pw = int(np.ceil(canvas_w / PAD) * PAD)
    ph = int(np.ceil(canvas_h / PAD) * PAD)
    lay = spine.Layout(pw, ph)
    return {
        "slots": lay.n_slots,
        "bytes_total": lay.n_slots * BS,
        "pages_x": lay.pages_x,
        "pages_y": lay.pages_y,
        "canvas": (pw, ph),
    }


# -------------------------------------------------------------------- encode

@dataclass
class EncodeReport:
    canvas: tuple
    original: tuple
    slots: int
    capacity_bytes: int
    layers: list = field(default_factory=list)
    redundancy: list = field(default_factory=list)

    def as_text(self) -> str:
        lines = [
            f"canvas         : {self.canvas[0]}x{self.canvas[1]}   original {self.original[0]}x{self.original[1]}",
            f"page slots     : {self.slots}   ({self.capacity_bytes} B of symbol capacity)",
        ]
        for i, (d, r) in enumerate(zip(self.layers, self.redundancy)):
            lines.append(
                f"layer {i}        : {d['w']}x{d['h']}  {d['plain_bytes']} B plaintext -> "
                f"{d['symbols']} symbols (x{r:.2f} redundancy)"
            )
        return "\n".join(lines)


def encode(src: Image.Image, password: str, *, alpha_detail: float = 3.2,
           delta=qim.DEFAULT_DELTA, lossless: bool = False
           ) -> tuple[Image.Image, EncodeReport]:
    """Scramble `src` into a viewable carrier.

    With `lossless=True` only the tile permutation is applied: the round trip is
    then bit-exact, but nothing is embedded, so damage tolerance is limited to
    "intact tiles survive" and rescaling is not recoverable.
    """
    master = crypto.master_key(password)
    fseed = crypto.fountain_seed(master)
    tseed = crypto.tile_seed(master)
    sync = crypto.sync_word(master)

    rgb = np.asarray(src.convert("RGB"))
    oh, ow = rgb.shape[:2]
    # Pad to whole pages, not just whole tiles: any strip narrower than 128 px
    # after the last full page would carry no pages at all and its capacity
    # would be wasted.  The original size travels in the header, so the decoder
    # crops the padding away again.  Lossless carriers carry no header, so they
    # pad to a whole tile only -- a whole page would just enlarge the output.
    pad = TILE if lossless else PAD
    padded = _pad_to(rgb, pad, pad)
    ph, pw = padded.shape[:2]
    lay = spine.Layout(pw, ph)
    slots = lay.n_slots
    if slots < 4:
        raise ValueError(
            f"image too small ({ow}x{oh} -> {slots} pages). Need >= 4 pages, "
            "i.e. about 256x256 pixels."
        )

    pub = src.convert("RGB")
    reg1 = os.urandom(8)

    if lossless:
        carrier = tiles.scramble(padded, tseed, TILE)
        return Image.fromarray(np.ascontiguousarray(carrier)), EncodeReport(
            canvas=(pw, ph), original=(ow, oh), slots=slots,
            capacity_bytes=0, layers=[], redundancy=[],
        )

    # Pick the thumbnail by the reliability we want, not the other way round:
    # take the best thumbnail whose symbol count still leaves `alpha_detail`x
    # redundancy, so the promised damage tolerance is actually delivered.
    blob0 = th0 = None
    K0 = 0
    for cand_blob, cand_th in _thumb_iter(pub):
        k = int(np.ceil((PUB_LEN + 32 + len(cand_blob)) / BS))
        if k * alpha_detail <= slots:
            blob0, th0, K0 = cand_blob, cand_th, k
            break
    if blob0 is None:
        blob0, th0 = _pick_thumb(pub, 120)
        K0 = min(slots, int(np.ceil((PUB_LEN + 32 + len(blob0)) / BS)))
    total0 = slots
    sealed0 = crypto.seal(master, blob0, aad=reg1)

    body = bytearray(
        PUB.pack(
            MAGIC, 1,
            pw & 0xFFFF, ph & 0xFFFF, ow & 0xFFFF, oh & 0xFFFF,
            TILE, qim.QUALITY & 0xFF,
            len(sealed0) & 0xFFFF, reg1, 0,
        )
    )
    body[-2:] = spine.crc16(bytes(body[:-2])).to_bytes(2, "big")

    plain0 = bytes(body) + sealed0
    K0 = int(np.ceil(len(plain0) / BS))
    if K0 > total0:
        raise ValueError(
            f"payload needs {K0} symbols but the canvas only has {total0} pages; "
            "use a larger image"
        )

    blocks0 = fountain.split(plain0, K0, BS)

    rng = np.random.Generator(np.random.PCG64(int(fseed) & 0xFFFF_FFFF))
    order = rng.permutation(slots)

    pages = {}
    for pos, idx in zip(order, range(total0)):
        px, py = lay.slot_xy(int(pos))
        chunk = fountain.make_symbol(blocks0, idx, fseed)
        pages[int(pos)] = spine.encode_page(K0, idx, chunk, px, py, sync)

    bits = lay.build_bits(pages)

    carrier = tiles.scramble(padded, tseed, TILE)
    y = _luma(carrier)
    y2 = qim.embed(y, bits, delta)
    out = np.clip(np.rint(carrier.astype(np.float32) + (y2 - y)[..., None]), 0, 255).astype(np.uint8)

    rep = EncodeReport(
        canvas=(pw, ph), original=(ow, oh), slots=slots, capacity_bytes=slots * BS,
        layers=[{"w": th0.size[0], "h": th0.size[1], "plain_bytes": len(plain0),
                 "symbols": total0}],
        redundancy=[total0 / max(K0, 1)],
    )
    return Image.fromarray(out), rep


# -------------------------------------------------------------------- decode

@dataclass
class DecodeReport:
    ok: bool
    reason: str = ""
    scale: tuple = (1.0, 1.0)
    pages_total: int = 0
    pages_ok: int = 0
    layers: int = 0
    seam: float = 1.0
    mode: str = "none"
    coverage: float = 0.0
    crop_offset: tuple = (0, 0)
    canvas: tuple = (0, 0)
    thumb: tuple = (0, 0)

    def as_text(self) -> str:
        if not self.ok:
            return f"FAILED: {self.reason}"
        return (
            f"pages {self.pages_ok}/{self.pages_total} valid  layers={self.layers}  "
            f"scale=({self.scale[0]:.3f},{self.scale[1]:.3f})  seam={self.seam:.2f}\n"
            f"mode={self.mode}  coverage={self.coverage:.0%}  crop={self.crop_offset}  "
            f"canvas={self.canvas}  coarse={self.thumb[0]}x{self.thumb[1]}"
        )


def _page_block(bits: np.ndarray, kx: int, ky: int, px: int, py: int):
    y0 = ky + py * spine.PAGE_BLOCKS
    x0 = kx + px * spine.PAGE_BLOCKS
    if y0 + spine.PAGE_BLOCKS > bits.shape[0] or x0 + spine.PAGE_BLOCKS > bits.shape[1]:
        return None
    return bits[y0:y0 + spine.PAGE_BLOCKS, x0:x0 + spine.PAGE_BLOCKS]


def _probe(bits: np.ndarray, kx: int, ky: int, px: int, py: int, sync: int):
    blk = _page_block(bits, kx, ky, px, py)
    if blk is None:
        return None
    return spine.decode_page(blk, sync)


def _find_phase(bits: np.ndarray, sync: int):
    """Locate the page framing: a 16x16 grid of candidates checked by sync word."""
    nby = bits.shape[0] // spine.PAGE_BLOCKS
    nbx = bits.shape[1] // spine.PAGE_BLOCKS
    if nby < 1 or nbx < 1:
        return None
    probes = [(0, 0), (min(1, nbx - 1), 0), (0, min(1, nby - 1)),
              (min(2, nbx - 1), min(2, nby - 1))]
    single = None
    for ky in range(spine.PAGE_BLOCKS):
        for kx in range(spine.PAGE_BLOCKS):
            hits = 0
            for px, py in probes:
                blk = _page_block(bits, kx, ky, px, py)
                if blk is not None and spine.sync_ok(blk, sync):
                    hits += 1
                    if hits >= 2:
                        return kx, ky
            if hits == 1 and single is None:
                single = (kx, ky)
    return single


def _harvest(bits: np.ndarray, kx: int, ky: int, sync: int):
    h, w = bits.shape[0], bits.shape[1]
    out = {}
    npy = (h - ky) // spine.PAGE_BLOCKS
    npx = (w - kx) // spine.PAGE_BLOCKS
    for py in range(npy):
        for px in range(npx):
            r = _probe(bits, kx, ky, px, py, sync)
            if r is not None:
                out[(py, px)] = r
    return out, npy * npx


_FACTORS = (1.0, 0.25)


def candidates(gray: np.ndarray, max_pairs: int = 16, per_axis: int = 6):
    """Lattice hypotheses (block_x, phase_x, block_y, phase_y) in received px.

    The carrier has an 8-pixel block comb *and* a 32-pixel tile comb, and which
    one dominates depends on the picture, so each detected period is expanded
    by both readings: the period itself (it *is* the block grid) and a quarter
    of it (it is the 4-block tile grid).  Every hypothesis is cheap to reject
    (the page sync word is a 16-bit test) and definitive when it works, so
    trying a dozen is faster and far more reliable than guessing.
    """
    out = [(float(qim.BLOCK), 0.0, float(qim.BLOCK), 0.0)]

    def axis(peaks):
        hyps = []
        for period, phase, strength in peaks[:3]:
            for f in _FACTORS:
                bp = period * f
                if not (2.0 <= bp <= 48.0):
                    continue
                hyps.append((float(bp), float(phase % bp), float(strength)))
        return hyps[:per_axis]

    try:
        px, py = geom.estimate(gray, qim.BLOCK)
    except Exception:
        return out
    hx = axis(px) or [(float(qim.BLOCK), 0.0, 1.0)]
    hy = axis(py) or [(float(qim.BLOCK), 0.0, 1.0)]
    pairs = []
    for ix, (bx, fx, _sx) in enumerate(hx):
        for iy, (by, fy, _sy) in enumerate(hy):
            pairs.append((ix + iy, bx, fx, by, fy))
    pairs.sort(key=lambda t: t[0])
    for _r, bx, fx, by, fy in pairs[:max_pairs]:
        out.append((bx, fx, by, fy))
    return out


def _match_score(fine: np.ndarray, coarse: np.ndarray, mask: np.ndarray) -> float:
    """Correlation between the un-permuted carrier and the coarse thumbnail.

    This is the honest test of "did the permutation come out right?".  Tile-seam
    energy is not usable: a JPEG re-encode leaves ringing on every tile border
    of a *correctly* reassembled picture, which looks just like a wrong one.
    Correlating against the thumbnail the decoder already recovered is direct
    evidence, and it still works when part of the frame was cropped away.
    """
    def small(a):
        im = Image.fromarray(np.ascontiguousarray(a))
        return np.asarray(im.resize((40, 40), Image.BOX)).astype(np.float32).mean(axis=2)

    a, b = small(fine), small(coarse)
    m = small((mask.astype(np.uint8) * 255)[..., None].repeat(3, axis=2)) > 127
    if m.sum() < 40:
        return 0.0
    a = a[m] - a[m].mean()
    b = b[m] - b[m].mean()
    den = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float((a * b).sum() / den) if den > 1e-9 else 0.0


def decode(src: Image.Image, password: str, *, delta=qim.DEFAULT_DELTA,
           force: str = "auto", max_candidates: int = 16):
    master = crypto.master_key(password)
    fseed = crypto.fountain_seed(master)
    tseed = crypto.tile_seed(master)
    sync = crypto.sync_word(master)

    rgb = np.asarray(src.convert("RGB"))
    gray = _luma(rgb)
    cands = candidates(gray)[:max_candidates]
    if not cands:
        return None, DecodeReport(False, "no periodic lattice found")

    last = DecodeReport(False, "no page decoded")
    for pxx, phx, pyy, phy in cands:
        canvas, sx, sy = geom.to_canvas(rgb, pxx, pyy, phx, phy)
        if canvas is None:
            continue
        bits = qim.extract(_luma(canvas), delta)
        phase = _find_phase(bits, sync)
        if phase is None:
            continue
        kx, ky = phase
        found, total = _harvest(bits, kx, ky, sync)
        if not found:
            continue

        votes = {}
        groups = {}
        for (rpy, rpx), rec in found.items():
            k, idx, payload, gpx, gpy = rec
            mx = kx + spine.PAGE_BLOCKS * (rpx - gpx)
            my = ky + spine.PAGE_BLOCKS * (rpy - gpy)
            votes[(mx, my)] = votes.get((mx, my), 0) + 1
            groups.setdefault(k, {})[idx] = payload
        (mx, my), _v = max(votes.items(), key=lambda t: t[1])

        plain = None
        if len(groups) == 1:
            k, received = next(iter(groups.items()))
            blocks = fountain.decode(received, k, BS, fseed)
            if blocks is not None:
                plain = fountain.join(blocks, k * BS)
        if plain is None:
            last = DecodeReport(False, "payload not recoverable",
                                scale=(sx, sy), pages_total=total, pages_ok=len(found))
            continue

        body = plain[:PUB_LEN]
        (magic, ver, cw, ch, ow, oh, tile, _quality, len0, reg1, _crc) = PUB.unpack(body)
        if magic != MAGIC or spine.crc16(body[:-2]) != int.from_bytes(body[-2:], "big"):
            last = DecodeReport(False, "corrupt header")
            continue

        blob0 = crypto.open_(master, plain[PUB_LEN:PUB_LEN + len0], aad=reg1)
        if blob0 is None:
            last = DecodeReport(False, "authentication failed (wrong passphrase?)")
            continue
        try:
            thumb0 = unpack_thumb(blob0)
        except Exception:
            last = DecodeReport(False, "undecodable thumbnail")
            continue

        # `m` says R block = canvas block + m, i.e. the received frame starts m
        # blocks *after* the canvas origin.  So the canvas coordinate of R's
        # top-left corner is -8*m, not +8*m.  With m == 0 (no crop) the sign is
        # invisible; for any cropped frame it shifted the result by a full page.
        cx, cy = -8 * mx, -8 * my
        C = np.zeros((ch, cw, 3), dtype=np.uint8)
        cover = np.zeros((ch, cw), dtype=bool)
        x_src, y_src = max(0, -cx), max(0, -cy)
        x_dst, y_dst = max(0, cx), max(0, cy)
        ww = min(canvas.shape[1] - x_src, cw - x_dst)
        hh = min(canvas.shape[0] - y_src, ch - y_dst)
        if ww <= 0 or hh <= 0:
            last = DecodeReport(False, "geometry outside canvas")
            continue
        C[y_dst:y_dst + hh, x_dst:x_dst + ww] = canvas[y_src:y_src + hh, x_src:x_src + ww]
        cover[y_dst:y_dst + hh, x_dst:x_dst + ww] = True

        fine = tiles.unscramble(C, tseed, TILE)
        # The coverage mask describes the *received* frame; the reconstructed
        # picture keeps exactly the tiles that were visible, which the
        # un-permutation scatters across the canvas.  Un-permute the mask too,
        # otherwise the correlation test compares valid detail against holes.
        fmask = tiles.unscramble(cover.astype(np.uint8) * 255, tseed, TILE) > 127
        seam = tiles.seam_score(fine, TILE)

        coarse_src = thumb0
        # The thumbnail describes the *original* picture, which occupies the
        # canvas from (0,0).  Upscaling it to the canvas size and then cropping
        # would stretch it by the padding ratio and shift it at the far edge, so
        # place it at the original size inside a canvas-sized buffer instead.
        coarse = np.zeros((ch, cw, 3), dtype=np.uint8)
        coarse[:oh, :ow] = np.asarray(_upscale(coarse_src, (ow, oh)).convert("RGB"))
        fine_c = fine[:oh, :ow]
        coarse_c = coarse[:oh, :ow]
        fm_c = fmask[:oh, :ow]
        match = _match_score(fine, coarse, fmask)
        # If most pages were destroyed, the mosaic itself is gone: the scrambled
        # tile fragments would come back as confetti, so prefer the coherent
        # (blurrier) thumbnail.  Cropped images still show 100% here because the
        # missing pages are simply outside the frame.
        intact = len(found) / max(total, 1)

        if force == "coarse":
            out, mode = coarse_c, "coarse"
        elif force == "fine":
            out, mode = fine_c, "fine"
        elif match > 0.45 and intact >= 0.5:
            out, mode = fine_c, "fine"
            missing = ~fm_c
            frac = float(missing.mean())
            if frac >= 0.8:
                # Almost the whole canvas was never covered: the geometry is
                # not trustworthy enough to keep mosaic detail.
                out, mode = coarse_c, "coarse"
            elif frac > 0.0:
                out = out.copy()
                out[missing] = coarse_c[missing]
                mode = "fine+coarse"
        else:
            out, mode = coarse_c, "coarse"

        rep = DecodeReport(
            True, "", scale=(sx, sy), pages_total=total, pages_ok=len(found),
            layers=len(plain), seam=seam, mode=mode,
            coverage=float(fm_c.mean()), crop_offset=(cx, cy), canvas=(cw, ch),
            thumb=coarse_src.size,
        )
        img = Image.fromarray(np.ascontiguousarray(out))
        setattr(img, "revimg_coarse", Image.fromarray(np.ascontiguousarray(coarse_c)))
        setattr(img, "revimg_fine", Image.fromarray(np.ascontiguousarray(fine_c)))
        return img, rep

    mosaic = _mosaic_only(rgb, candidates(gray, max_pairs=36, per_axis=6), tseed)
    if mosaic is not None:
        return mosaic
    return None, last


def _mosaic_fit(rgb: np.ndarray, bx: float, fx: float, by: float, fy: float,
                tseed: int, size, tile: int):
    """Score one lattice hypothesis by seam continuity after un-permuting."""
    canvas, _sx, _sy = geom.to_canvas(rgb, bx, by, fx, fy, size=size)
    if canvas is None:
        return None
    C = np.ascontiguousarray(canvas)
    scrambled = tiles.seam_score(C, tile)
    if scrambled <= 1.0:
        return None
    fine = tiles.unscramble(C, tseed, tile)
    return float(tiles.seam_score(fine, tile) / scrambled), 0.0, fine


def _mosaic_only(rgb: np.ndarray, cands, tseed: int, tile: int = TILE):
    """Last resort: no page survived, but the visible mosaic might still be a
    plain permutation of the original.

    This is what rescues a *downscaled* image.  Shrinking halves the block pitch,
    so the QIM lattice ends up at the Nyquist frequency and the payload is gone
    -- but the 32-pixel tiles were only scaled, not destroyed, and their content
    is a natural image patch.  If the frame was not cropped, the tile grid is
    anchored at the canvas origin, so the picture can be reassembled without any
    payload at all, at the resolution the attacker left us.

    The test is relative: un-permuting must make the picture markedly *smoother*
    across tile seams than the scrambled version.  A wrong key or wrong geometry
    leaves it scrambled, so this cannot fire on garbage.

    Known limit: this only recovers clean ratios (2:1, 1:2, 4:3-ish).  Measured
    with an exact known scale, arbitrary ratios reconstruct at 28-32 dB, so the
    information survives rescaling -- what fails is the *geometry estimate*:
    1.6% of period error across 64 tiles is a whole tile of drift.  A pattern
    search over (scale, phase) was tried and does not work yet: the seam ratio
    prefers a slightly blurred wrong lattice over the correct one, and a detail
    guard that rejects the blurred candidates also rejects correct 2x upscales.
    """
    best = None
    seen = set()
    t0 = time.time()
    w, h = rgb.shape[1], rgb.shape[0]
    for bx, fx, by, fy in cands:
        step = max(1.0, tile * bx / qim.BLOCK)
        fx = round(fx / step) * step
        fy = round(fy / step) * step
        cw, ch = geom.canvas_size(w, h, bx, by, fx, fy, qim.BLOCK, tile)
        # A hypothesis can imply a wildly wrong canvas (a 2-pixel period means an
        # 8x upscale); resampling to 8000x8000 just to score it is what made a
        # wrong passphrase take half a minute.  The true canvas is within ~3x of
        # what we received, so anything outside that is not worth testing.
        if not (max(2 * tile, w // 3, h // 3) <= cw <= 3 * max(w, h)):
            continue
        if not (max(2 * tile, w // 3, h // 3) <= ch <= 3 * max(w, h)):
            continue
        key = (cw, ch, round(bx, 2), round(by, 2), round(fx, 1), round(fy, 1))
        if key in seen:
            continue
        seen.add(key)
        r = _mosaic_fit(rgb, bx, fx, by, fy, tseed, (cw, ch), tile)
        if r is None or r[0] > 0.75:
            if time.time() - t0 > 8.0:
                break
            continue
        # Take the *first* hypothesis that explains the picture, not the lowest
        # score: a seam ratio rewards blur, so "lowest" keeps drifting towards
        # over-smoothed resamples (measured: it turned an exact lossless
        # round trip into a resampled one).  Candidates are ordered
        # identity-first, so the right answer is normally the first one.
        best = (r[0], r[2], (cw, ch))
        break
    if best is None:
        return None
    _ratio, img, dims = best
    return Image.fromarray(np.ascontiguousarray(img)), DecodeReport(
        True, "", mode="mosaic", canvas=dims)

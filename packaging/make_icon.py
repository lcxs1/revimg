# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 revimg contributors

"""Draw the application icon.

The mark is a scrambled mosaic seen through a padlock: the mosaic is what the
file looks like to everyone else, the lock is what the passphrase opens.  Drawn
at 4x and downsampled so it stays clean in the 16px taskbar size.

    python packaging/make_icon.py      # -> assets/revimg.ico, assets/revimg.png
"""

from __future__ import annotations

import pathlib

from PIL import Image, ImageDraw

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "assets"
SIZE = 256
SS = 4  # supersampling factor

TILES = (
    (58, 104, 170), (32, 156, 148), (214, 168, 66), (150, 72, 120),
    (76, 176, 112), (44, 86, 148), (196, 108, 72), (60, 132, 158),
    (178, 154, 72), (96, 84, 168), (40, 120, 122), (208, 146, 92),
    (120, 148, 70), (66, 70, 130), (188, 118, 150), (52, 158, 118),
)


def _gradient(size: int, top: tuple, bottom: tuple) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 255))
    draw = ImageDraw.Draw(img)
    for y in range(size):
        t = y / max(1, size - 1)
        colour = tuple(round(top[i] * (1 - t) + bottom[i] * t) for i in range(3))
        draw.line([(0, y), (size - 1, y)], fill=colour + (255,))
    return img


def build() -> Image.Image:
    side = SIZE * SS
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))

    # Rounded background with a vertical gradient.
    background = _gradient(side, (10, 18, 34), (30, 52, 86)).convert("RGBA")
    mask = Image.new("L", (side, side), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [8 * SS, 8 * SS, side - 8 * SS, side - 8 * SS], radius=54 * SS, fill=255
    )
    canvas.paste(background, (0, 0), mask)

    draw = ImageDraw.Draw(canvas)

    # Mosaic tiles: the visible scrambled carrier.
    left, top = 34 * SS, 34 * SS
    span = (SIZE - 68) * SS
    gap = 4 * SS
    tile = (span - gap * 3) / 4
    for row in range(4):
        for col in range(4):
            index = row * 4 + col
            if index == 5 or index == 10:
                continue  # two missing tiles make the damage story visible
            x0 = left + col * (tile + gap)
            y0 = top + row * (tile + gap)
            shade = 0.72 + 0.40 * (((index * 37) % 11) / 10)
            colour = tuple(min(255, round(c * shade)) for c in TILES[index % len(TILES)])
            draw.rounded_rectangle(
                [x0, y0, x0 + tile, y0 + tile], radius=6 * SS, fill=colour + (255,)
            )

    # A soft dark plate so the white padlock reads against any tile.
    draw.rounded_rectangle(
        [82 * SS, 92 * SS, 174 * SS, 182 * SS], radius=24 * SS, fill=(8, 14, 26, 205)
    )

    # Padlock shackle (top half of a ring) and body.
    ring = [105 * SS, 88 * SS, 151 * SS, 140 * SS]
    draw.arc(ring, start=180, end=360, fill=(242, 246, 252, 255), width=9 * SS)
    draw.rounded_rectangle(
        [96 * SS, 118 * SS, 160 * SS, 176 * SS], radius=12 * SS,
        fill=(242, 246, 252, 255),
    )
    draw.ellipse([120 * SS, 134 * SS, 136 * SS, 150 * SS], fill=(18, 30, 52, 255))
    draw.rounded_rectangle(
        [125 * SS, 144 * SS, 131 * SS, 160 * SS], radius=3 * SS, fill=(18, 30, 52, 255)
    )

    # Inner highlight border, then downsample for clean edges everywhere.
    draw.rounded_rectangle(
        [14 * SS, 14 * SS, side - 14 * SS, side - 14 * SS],
        radius=48 * SS, outline=(255, 255, 255, 46), width=2 * SS,
    )
    return canvas.resize((SIZE, SIZE), Image.LANCZOS)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    icon = build()
    png = OUT / "revimg.png"
    ico = OUT / "revimg.ico"
    icon.save(png)
    icon.save(ico, format="ICO", sizes=[(16, 16), (24, 24), (32, 32),
                                        (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"wrote {png}")
    print(f"wrote {ico}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

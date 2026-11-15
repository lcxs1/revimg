# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 revimg contributors

"""revimg - damage-tolerant reversible image scramble.

Quick start
-----------
    from revimg import encrypt_image, decrypt_image

    img, report = encrypt_image("photo.jpg", "passphrase")
    img.save("locked.png")                 # still an ordinary image

    restored, report = decrypt_image("locked.png", "passphrase")
    restored.save("restored.png")
"""

from .codec import DecodeReport, EncodeReport, capacity, decode, encode, unpack_thumb
from .tiles import seam_score, scramble, unscramble

__all__ = [
    "encrypt_image",
    "decrypt_image",
    "encode",
    "decode",
    "capacity",
    "unpack_thumb",
    "scramble",
    "unscramble",
    "seam_score",
    "EncodeReport",
    "DecodeReport",
]
__version__ = "1.0.0"


def encrypt_image(path, password, *, alpha=3.2, delta=None):
    """Encode an image file into a viewable, damage-tolerant carrier."""
    from PIL import Image

    with Image.open(path) as im:
        im.load()
        src = im.convert("RGB")
    kw = {} if delta is None else {"delta": delta}
    return encode(src, password, alpha_detail=alpha, **kw)


def decrypt_image(path, password, *, force="auto", delta=None):
    """Recover as much of the original as the surviving data allows."""
    from PIL import Image

    with Image.open(path) as im:
        im.load()
        src = im.convert("RGB")
    kw = {} if delta is None else {"delta": delta}
    return decode(src, password, force=force, **kw)

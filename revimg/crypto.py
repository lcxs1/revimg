# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 revimg contributors

"""Key derivation and authenticated encryption (standard library only).

Design notes
------------
* One PBKDF2-HMAC-SHA256 pass turns the passphrase into a 64-byte master key.
  No second expensive KDF is needed: per-message subkeys are cheap HMACs of the
  master key with a context label and the message's random nonce.
* Confidentiality: SHAKE-256 in counter mode acts as a stream cipher over the
  plaintext (a standard XOF construction, keyed with a per-message subkey and
  the nonce).
* Integrity: HMAC-SHA256 over (associated data || ciphertext), truncated to
  128 bits.

This is an encrypt-then-MAC construction; both operations use independent keys.
"""

from __future__ import annotations

import hashlib
import hmac
import os

PBKDF2_ROUNDS = 200_000
_MAGIC = b"revimg-v1"


def master_key(passphrase: str | bytes, rounds: int = PBKDF2_ROUNDS) -> bytes:
    if isinstance(passphrase, str):
        passphrase = passphrase.encode("utf-8")
    if not passphrase:
        raise ValueError("empty passphrase")
    return hashlib.pbkdf2_hmac("sha256", passphrase, _MAGIC, rounds, 64)


def fountain_seed(master: bytes) -> int:
    """Deterministic 63-bit seed for the erasure code's coefficient generator."""
    d = hmac.new(master, b"fountain", hashlib.sha256).digest()
    return int.from_bytes(d[:8], "big") & ((1 << 63) - 1)


def tile_seed(master: bytes) -> int:
    d = hmac.new(master, b"tiles", hashlib.sha256).digest()
    return int.from_bytes(d[:8], "big") & ((1 << 63) - 1)


def sync_word(master: bytes) -> int:
    """16-bit page sync pattern, derived from the passphrase.

    Kept key-dependent so an attacker cannot cheaply locate the payload pages
    without knowing the passphrase.
    """
    d = hmac.new(master, b"pagesync", hashlib.sha256).digest()
    return int.from_bytes(d[:2], "big")


def _subkeys(master: bytes, nonce: bytes):
    enc = hmac.new(master, b"enc|" + nonce, hashlib.sha256).digest()
    mac = hmac.new(master, b"mac|" + nonce, hashlib.sha256).digest()
    return enc, mac


def _ks(key: bytes, n: int) -> bytes:
    return hashlib.shake_256(key).digest(n)


def seal(master: bytes, plaintext: bytes, aad: bytes = b"") -> bytes:
    """Return nonce || ciphertext || tag."""
    nonce = os.urandom(16)
    enc, mac = _subkeys(master, nonce)
    ct = bytes(a ^ b for a, b in zip(plaintext, _ks(enc, len(plaintext))))
    tag = hmac.new(mac, aad + nonce + ct, hashlib.sha256).digest()[:16]
    return nonce + ct + tag


def open_(master: bytes, blob: bytes, aad: bytes = b"") -> bytes | None:
    if len(blob) < 32:
        return None
    nonce, ct, tag = blob[:16], blob[16:-16], blob[-16:]
    enc, mac = _subkeys(master, nonce)
    if not hmac.compare_digest(tag, hmac.new(mac, aad + nonce + ct, hashlib.sha256).digest()[:16]):
        return None
    return bytes(a ^ b for a, b in zip(ct, _ks(enc, len(ct))))


def aead_size(n: int) -> int:
    return n + 32

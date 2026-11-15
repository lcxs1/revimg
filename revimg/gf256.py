# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 revimg contributors

"""GF(256) arithmetic and a Reed-Solomon codec (errors, no erasures needed).

Field: GF(2^8) with primitive polynomial x^8 + x^4 + x^3 + x^2 + 1 (0x11D),
generator alpha = 2.  Codewords are systematic: message bytes first, then
`nsym` parity bytes.  Polynomials are lists of coefficients, highest degree
first, i.e. `[c0, c1, ..., cn]` means c0*x^n + ... + cn.

The decoder uses no polynomial division: Berlekamp-Massey finds the error
locator, a Chien search turns it into positions, and the error magnitudes are
obtained by solving the (small) Vandermonde system implied by the syndromes.
"""

from __future__ import annotations

PRIM = 0x11D

EXP = [0] * 512
LOG = [0] * 256
_x = 1
for _i in range(255):
    EXP[_i] = _x
    LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= PRIM
for _i in range(255, 512):
    EXP[_i] = EXP[_i - 255]


class ReedSolomonError(ValueError):
    pass


def gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return EXP[LOG[a] + LOG[b]]


def gf_div(a: int, b: int) -> int:
    if b == 0:
        raise ZeroDivisionError("division by zero in GF(256)")
    if a == 0:
        return 0
    return EXP[(LOG[a] - LOG[b]) % 255]


def gf_pow(a: int, p: int) -> int:
    if a == 0:
        return 0 if p > 0 else 1
    return EXP[(LOG[a] * p) % 255]


def gf_inv(a: int) -> int:
    if a == 0:
        raise ZeroDivisionError("inverse of zero")
    return EXP[(255 - LOG[a]) % 255]


# ---------------------------------------------------------------- polynomials

def poly_scale(p, s):
    if s == 0:
        return [0] * len(p)
    return [gf_mul(c, s) for c in p]


def poly_add(p, q):
    n = max(len(p), len(q))
    r = [0] * n
    o = n - len(p)
    for i, c in enumerate(p):
        r[i + o] ^= c
    o = n - len(q)
    for i, c in enumerate(q):
        r[i + o] ^= c
    return r


def poly_mul(p, q):
    r = [0] * (len(p) + len(q) - 1)
    for i, pc in enumerate(p):
        if pc == 0:
            continue
        for j, qc in enumerate(q):
            if qc:
                r[i + j] ^= gf_mul(pc, qc)
    return r


def poly_eval(p, x):
    if not p:
        return 0
    y = p[0]
    for c in p[1:]:
        y = gf_mul(y, x) ^ c
    return y


# ------------------------------------------------------------------- encoder

_GEN_CACHE = {}


def generator_poly(nsym: int):
    if nsym in _GEN_CACHE:
        return _GEN_CACHE[nsym]
    g = [1]
    for i in range(nsym):
        g = poly_mul(g, [1, gf_pow(2, i)])
    _GEN_CACHE[nsym] = g
    return g


def rs_encode(msg: bytes, nsym: int) -> bytes:
    """Return msg + parity (systematic). len(msg) + nsym <= 255."""
    if len(msg) + nsym > 255:
        raise ValueError("codeword too long for GF(256)")
    gen = generator_poly(nsym)
    nmsg = len(msg)
    out = list(msg) + [0] * (len(gen) - 1)
    for i in range(nmsg):
        coef = out[i]
        if coef:
            for j in range(1, len(gen)):
                out[i + j] ^= gf_mul(gen[j], coef)
    # Synthetic division overwrites the message window; the systematic part is
    # the original message followed by the remainder.
    return bytes(msg) + bytes(out[nmsg:])


# ------------------------------------------------------------------- decoder

def syndromes(msg, nsym: int):
    return [poly_eval(msg, gf_pow(2, i)) for i in range(nsym)]


def find_error_locator(synd, nsym: int):
    """Berlekamp-Massey.  Returns the error locator polynomial, high deg first."""
    err_loc = [1]
    old_loc = [1]
    for i in range(nsym):
        delta = synd[i]
        for j in range(1, len(err_loc)):
            delta ^= gf_mul(err_loc[-(j + 1)], synd[i - j])
        old_loc = old_loc + [0]
        if delta:
            if len(old_loc) > len(err_loc):
                new_loc = poly_scale(old_loc, delta)
                old_loc = poly_scale(err_loc, gf_inv(delta))
                err_loc = new_loc
            err_loc = poly_add(err_loc, poly_scale(old_loc, delta))
    while len(err_loc) > 1 and err_loc[0] == 0:
        err_loc.pop(0)
    return err_loc


def find_error_positions(err_loc, nmess: int):
    """Chien search.  Returns positions (0 = first byte) or None."""
    nerr = len(err_loc) - 1
    if nerr == 0:
        return []
    rev = err_loc[::-1]
    pos = []
    for i in range(nmess):
        if poly_eval(rev, gf_pow(2, i)) == 0:
            pos.append(nmess - 1 - i)
    if len(pos) != nerr:
        return None
    if len(set(pos)) != len(pos):
        return None
    return pos


def _solve_magnitudes(synd, positions, nmess: int):
    """Solve S_i = sum_j e_j X_j^i for the error magnitudes e_j."""
    t = len(positions)
    X = [gf_pow(2, nmess - 1 - p) for p in positions]
    # Augmented matrix of the t x t Vandermonde system, i = 0..t-1.
    rows = []
    for i in range(t):
        row = [gf_pow(X[j], i) for j in range(t)]
        row.append(synd[i])
        rows.append(row)
    for col in range(t):
        piv = None
        for r in range(col, t):
            if rows[r][col]:
                piv = r
                break
        if piv is None:
            return None
        rows[col], rows[piv] = rows[piv], rows[col]
        inv = gf_inv(rows[col][col])
        if inv != 1:
            rows[col] = [gf_mul(v, inv) for v in rows[col]]
        for r in range(t):
            if r != col and rows[r][col]:
                f = rows[r][col]
                rows[r] = [a ^ gf_mul(f, b) for a, b in zip(rows[r], rows[col])]
    return [rows[i][t] for i in range(t)]


def rs_decode(codeword, nsym: int):
    """Correct up to nsym//2 byte errors.  Returns bytes, or None if hopeless."""
    msg = bytearray(codeword)
    n = len(msg)
    if n > 255:
        return None
    synd = syndromes(msg, nsym)
    if not any(synd):
        return bytes(msg)
    err_loc = find_error_locator(synd, nsym)
    nerr = len(err_loc) - 1
    if nerr == 0 or 2 * nerr > nsym:
        return None
    pos = find_error_positions(err_loc, n)
    if pos is None or len(pos) != nerr:
        return None
    mag = _solve_magnitudes(synd, pos, n)
    if mag is None:
        return None
    for p, e in zip(pos, mag):
        msg[p] ^= e
    if any(syndromes(msg, nsym)):
        return None
    return bytes(msg)


def rs_check(codeword, nsym: int) -> bool:
    return not any(syndromes(codeword, nsym))

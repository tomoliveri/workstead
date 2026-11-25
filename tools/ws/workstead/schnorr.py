"""BIP-340 Schnorr signatures over secp256k1, in pure Python (stdlib only).

This is what Nostr (NIP-01) uses to sign events. Workers run it on any OS
with no dependencies, so it is implemented here directly from BIP-340, with
Jacobian coordinates and a precomputed table for the generator to keep
signing and verification to a few milliseconds.

Verified against the official BIP-340 test vectors (tests/test_schnorr.py).
Secret keys come from os.urandom and nonces are derived as BIP-340 specifies
(deterministic from the key, message and fresh auxiliary randomness), so a
broken system RNG can't leak the key through nonce reuse.

Not constant time: fine for signing coordination events on a worker's own
machine, not for a shared HSM. Keep secret keys in the 0600 key file.
"""
from __future__ import annotations

import hashlib
import os
from typing import Optional, Tuple

P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8

Jac = Tuple[int, int, int]  # (X, Y, Z); Z == 0 is the point at infinity
INF: Jac = (1, 1, 0)


def _double(p: Jac) -> Jac:
    x, y, z = p
    if z == 0 or y == 0:
        return INF
    yy = y * y % P
    s = 4 * x * yy % P
    m = 3 * x * x % P  # a = 0 on secp256k1
    x3 = (m * m - 2 * s) % P
    y3 = (m * (s - x3) - 8 * yy * yy) % P
    z3 = 2 * y * z % P
    return x3, y3, z3


def _add(p: Jac, q: Jac) -> Jac:
    if p[2] == 0:
        return q
    if q[2] == 0:
        return p
    x1, y1, z1 = p
    x2, y2, z2 = q
    z1z1 = z1 * z1 % P
    z2z2 = z2 * z2 % P
    u1 = x1 * z2z2 % P
    u2 = x2 * z1z1 % P
    s1 = y1 * z2 * z2z2 % P
    s2 = y2 * z1 * z1z1 % P
    if u1 == u2:
        return _double(p) if s1 == s2 else INF
    h = (u2 - u1) % P
    r = (s2 - s1) % P
    hh = h * h % P
    hhh = h * hh % P
    v = u1 * hh % P
    x3 = (r * r - hhh - 2 * v) % P
    y3 = (r * (v - x3) - s1 * hhh) % P
    z3 = h * z1 * z2 % P
    return x3, y3, z3


def _affine(p: Jac) -> Optional[Tuple[int, int]]:
    if p[2] == 0:
        return None
    zi = pow(p[2], P - 2, P)
    zi2 = zi * zi % P
    return p[0] * zi2 % P, p[1] * zi2 * zi % P


def _mul(p: Jac, k: int) -> Jac:
    r = INF
    while k:
        if k & 1:
            r = _add(r, p)
        p = _double(p)
        k >>= 1
    return r


_G_TABLE = []
_g = (GX, GY, 1)
for _ in range(256):
    _G_TABLE.append(_g)
    _g = _double(_g)


def _mul_g(k: int) -> Jac:
    r = INF
    i = 0
    while k:
        if k & 1:
            r = _add(r, _G_TABLE[i])
        k >>= 1
        i += 1
    return r


def point_mul_g(k: int) -> Optional[Tuple[int, int]]:
    return _affine(_mul_g(k % N))


def tagged_hash(tag: str, msg: bytes) -> bytes:
    t = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(t + t + msg).digest()


def _int(b: bytes) -> int:
    return int.from_bytes(b, "big")


def _bytes(x: int) -> bytes:
    return x.to_bytes(32, "big")


def lift_x(x: int) -> Optional[Tuple[int, int]]:
    if x >= P:
        return None
    y_sq = (pow(x, 3, P) + 7) % P
    y = pow(y_sq, (P + 1) // 4, P)
    if y * y % P != y_sq:
        return None
    return x, (y if y % 2 == 0 else P - y)


def pubkey(seckey: bytes) -> bytes:
    """The 32-byte x-only public key for a 32-byte secret key."""
    d = _int(seckey)
    if not 1 <= d <= N - 1:
        raise ValueError("secret key out of range")
    return _bytes(point_mul_g(d)[0])


def sign(msg: bytes, seckey: bytes, aux: Optional[bytes] = None) -> bytes:
    """A 64-byte BIP-340 signature of a 32-byte message."""
    if len(msg) != 32:
        raise ValueError("BIP-340 signs 32-byte messages (Nostr signs the event id)")
    aux = os.urandom(32) if aux is None else aux
    d0 = _int(seckey)
    if not 1 <= d0 <= N - 1:
        raise ValueError("secret key out of range")
    px, py = point_mul_g(d0)
    d = d0 if py % 2 == 0 else N - d0
    t = bytes(a ^ b for a, b in zip(_bytes(d), tagged_hash("BIP0340/aux", aux)))
    k0 = _int(tagged_hash("BIP0340/nonce", t + _bytes(px) + msg)) % N
    if k0 == 0:
        raise ValueError("nonce is zero (negligible probability)")
    rx, ry = point_mul_g(k0)
    k = k0 if ry % 2 == 0 else N - k0
    e = _int(tagged_hash("BIP0340/challenge", _bytes(rx) + _bytes(px) + msg)) % N
    sig = _bytes(rx) + _bytes((k + e * d) % N)
    if not verify(msg, _bytes(px), sig):
        raise RuntimeError("signature self-check failed")
    return sig


def verify(msg: bytes, pub: bytes, sig: bytes) -> bool:
    if len(pub) != 32 or len(sig) != 64 or len(msg) != 32:
        return False
    pt = lift_x(_int(pub))
    if pt is None:
        return False
    r, s = _int(sig[:32]), _int(sig[32:])
    if r >= P or s >= N:
        return False
    e = _int(tagged_hash("BIP0340/challenge", sig[:32] + pub + msg)) % N
    # R = s*G - e*P
    rj = _add(_mul_g(s), _mul((pt[0], P - pt[1], 1), e))
    ra = _affine(rj)
    return ra is not None and ra[1] % 2 == 0 and ra[0] == r


def generate() -> Tuple[bytes, bytes]:
    """(secret key, x-only public key) from the OS's secure RNG."""
    while True:
        sk = os.urandom(32)
        if 1 <= _int(sk) <= N - 1:
            return sk, pubkey(sk)


# --- NIP-19 bech32 (npub / nsec) -------------------------------------------------

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(values) -> int:
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if ((b >> i) & 1) else 0
    return chk


def _hrp_expand(hrp: str):
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _convertbits(data, frombits: int, tobits: int, pad: bool = True):
    acc = bits = 0
    out = []
    maxv = (1 << tobits) - 1
    for value in data:
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            out.append((acc >> bits) & maxv)
    if pad and bits:
        out.append((acc << (tobits - bits)) & maxv)
    elif not pad and (bits >= frombits or ((acc << (tobits - bits)) & maxv)):
        raise ValueError("invalid padding")
    return out


def bech32_encode(hrp: str, data: bytes) -> str:
    d5 = _convertbits(data, 8, 5)
    poly = _polymod(_hrp_expand(hrp) + d5 + [0] * 6) ^ 1
    checksum = [(poly >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_CHARSET[x] for x in d5 + checksum)


def bech32_decode(s: str) -> Tuple[str, bytes]:
    s = s.lower()
    pos = s.rfind("1")
    if pos < 1 or pos + 7 > len(s):
        raise ValueError("not bech32")
    hrp, data = s[:pos], [_CHARSET.find(c) for c in s[pos + 1:]]
    if -1 in data or _polymod(_hrp_expand(hrp) + data) != 1:
        raise ValueError("bad bech32 checksum")
    return hrp, bytes(_convertbits(data[:-6], 5, 8, pad=False))


def npub(pub: bytes) -> str:
    return bech32_encode("npub", pub)


def nsec(sec: bytes) -> str:
    return bech32_encode("nsec", sec)


def parse_pubkey(s: str) -> str:
    """64-char hex from hex or npub1...; raises ValueError otherwise."""
    s = s.strip()
    if s.startswith("npub1"):
        hrp, data = bech32_decode(s)
        if hrp != "npub" or len(data) != 32:
            raise ValueError("bad npub")
        return data.hex()
    if len(s) == 64 and all(c in "0123456789abcdef" for c in s.lower()):
        return s.lower()
    raise ValueError(f"not a public key: {s[:20]}")

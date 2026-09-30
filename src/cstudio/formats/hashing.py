"""Hash functions used by Crimson Desert archives and Wwise.

* ``hashlittle`` - Bob Jenkins' lookup3, used by the PAZ key derivation.
* ``wwise_fnv1_32`` - the 32-bit FNV-1 hash Wwise uses to turn object names
  into short IDs (event, bank, state, switch and bus names). It lets names be
  *verified* deterministically: a proposed name either hashes to the ID or not.
"""

from __future__ import annotations

import struct

_MASK = 0xFFFFFFFF


def _rot(value: int, shift: int) -> int:
    return ((value << shift) | (value >> (32 - shift))) & _MASK


def hashlittle(data: bytes, initval: int = 0) -> int:
    length = len(data)
    a = b = c = (0xDEADBEEF + length + initval) & _MASK
    off = 0
    remaining = length
    while remaining > 12:
        a = (a + struct.unpack_from("<I", data, off)[0]) & _MASK
        b = (b + struct.unpack_from("<I", data, off + 4)[0]) & _MASK
        c = (c + struct.unpack_from("<I", data, off + 8)[0]) & _MASK
        a = (a - c) & _MASK; a ^= _rot(c, 4); c = (c + b) & _MASK
        b = (b - a) & _MASK; b ^= _rot(a, 6); a = (a + c) & _MASK
        c = (c - b) & _MASK; c ^= _rot(b, 8); b = (b + a) & _MASK
        a = (a - c) & _MASK; a ^= _rot(c, 16); c = (c + b) & _MASK
        b = (b - a) & _MASK; b ^= _rot(a, 19); a = (a + c) & _MASK
        c = (c - b) & _MASK; c ^= _rot(b, 4); b = (b + a) & _MASK
        off += 12
        remaining -= 12
    if remaining == 0:
        return c
    tail = data[off:] + b"\x00" * 12
    if remaining >= 12:
        c = (c + struct.unpack_from("<I", tail, 8)[0]) & _MASK
    elif remaining >= 9:
        c = (c + (struct.unpack_from("<I", tail, 8)[0] & (_MASK >> (8 * (12 - remaining))))) & _MASK
    if remaining >= 8:
        b = (b + struct.unpack_from("<I", tail, 4)[0]) & _MASK
    elif remaining >= 5:
        b = (b + (struct.unpack_from("<I", tail, 4)[0] & (_MASK >> (8 * (8 - remaining))))) & _MASK
    if remaining >= 4:
        a = (a + struct.unpack_from("<I", tail, 0)[0]) & _MASK
    else:
        a = (a + (struct.unpack_from("<I", tail, 0)[0] & (_MASK >> (8 * (4 - remaining))))) & _MASK
    c ^= b; c = (c - _rot(b, 14)) & _MASK
    a ^= c; a = (a - _rot(c, 11)) & _MASK
    b ^= a; b = (b - _rot(a, 25)) & _MASK
    c ^= b; c = (c - _rot(b, 16)) & _MASK
    a ^= c; a = (a - _rot(c, 4)) & _MASK
    b ^= a; b = (b - _rot(a, 14)) & _MASK
    c ^= b; c = (c - _rot(b, 24)) & _MASK
    return c


_FNV_OFFSET = 2166136261
_FNV_PRIME = 16777619


def wwise_fnv1_32(name: str) -> int:
    """Wwise short ID of ``name`` (FNV-1, 32-bit, over the lowercased name)."""

    value = _FNV_OFFSET
    for byte in name.lower().encode("utf-8"):
        value = (value * _FNV_PRIME) & _MASK
        value ^= byte
    return value


def wwise_name_matches(name: str, object_id: int) -> bool:
    return wwise_fnv1_32(name) == (object_id & _MASK)

"""Bounds-checked little-endian reader used by all binary parsers."""

from __future__ import annotations

import struct


class ParseError(ValueError):
    """Raised when a structure does not fit the bytes available."""


class BinaryReader:
    __slots__ = ("data", "pos", "end")

    def __init__(self, data: bytes | memoryview, pos: int = 0, end: int | None = None) -> None:
        self.data = data
        self.pos = pos
        self.end = len(data) if end is None else min(end, len(data))

    def remaining(self) -> int:
        return self.end - self.pos

    def _need(self, size: int) -> None:
        if size < 0 or self.pos + size > self.end:
            raise ParseError(f"need {size} bytes at 0x{self.pos:X}, only {self.end - self.pos} left")

    def skip(self, size: int) -> None:
        self._need(size)
        self.pos += size

    def bytes(self, size: int) -> bytes:
        self._need(size)
        value = bytes(self.data[self.pos : self.pos + size])
        self.pos += size
        return value

    def u8(self) -> int:
        self._need(1)
        value = self.data[self.pos]
        self.pos += 1
        return value

    def s8(self) -> int:
        value = self.u8()
        return value - 256 if value > 127 else value

    def _unpack(self, fmt: str, size: int):
        self._need(size)
        value = struct.unpack_from(fmt, self.data, self.pos)[0]
        self.pos += size
        return value

    def u16(self) -> int:
        return self._unpack("<H", 2)

    def s16(self) -> int:
        return self._unpack("<h", 2)

    def u32(self) -> int:
        return self._unpack("<I", 4)

    def s32(self) -> int:
        return self._unpack("<i", 4)

    def u64(self) -> int:
        return self._unpack("<Q", 8)

    def f32(self) -> float:
        return self._unpack("<f", 4)

    def f64(self) -> float:
        return self._unpack("<d", 8)

    def var(self) -> int:
        """Wwise variable-length integer (7 bits per byte, big-endian groups)."""

        cur = self.u8()
        value = cur & 0x7F
        loops = 0
        while cur & 0x80:
            loops += 1
            if loops > 5:
                raise ParseError("variable-length integer too long")
            cur = self.u8()
            value = (value << 7) | (cur & 0x7F)
        return value

    def strz(self, limit: int = 4096) -> str:
        start = self.pos
        stop = min(self.end, start + limit)
        idx = bytes(self.data[start:stop]).find(b"\x00")
        if idx < 0:
            raise ParseError(f"unterminated string at 0x{start:X}")
        value = bytes(self.data[start : start + idx]).decode("utf-8", errors="replace")
        self.pos = start + idx + 1
        return value

    def fourcc(self) -> str:
        return self.bytes(4).decode("ascii", errors="replace")

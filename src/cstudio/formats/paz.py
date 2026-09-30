"""Read-only access to Crimson Desert PAMT indexes and PAZ archives.

Layout knowledge comes from community research (see
``docs/research/crimson_desert_archives.md``):

* A package directory (``0000`` .. ``0035``) holds ``0.pamt`` (the index) and
  ``N.paz`` archives.
* ``meta/0.papgt`` lists the mounted package directories in priority order.
* Entries may be ChaCha20 encrypted (key/nonce derived from the lowercase
  basename via lookup3) and/or LZ4 compressed (block format, no frame).

This module never opens any game file for writing.
"""

from __future__ import annotations

import os
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Dict, List, Optional, Tuple

from .hashing import hashlittle

try:  # optional at import time so the pure parsers stay importable
    import lz4.block as _lz4_block
except ImportError:  # pragma: no cover - dependency is required in builds
    _lz4_block = None

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
except ImportError:  # pragma: no cover
    Cipher = None
    algorithms = None


CHACHA20_HASH_INITVAL = 0x000C5EDE
CHACHA20_KEY_XOR = 0x60616263
CHACHA20_XOR_DELTAS = (
    0x00000000, 0x0A0A0A0A, 0x0C0C0C0C, 0x06060606,
    0x0E0E0E0E, 0x0A0A0A0A, 0x06060606, 0x02020202,
)

COMPRESSION_LABELS = {0: "none", 1: "partial", 2: "lz4", 3: "zlib?", 4: "zlib/quicklz?"}
ENCRYPTION_LABELS = {0: "none", 1: "ice", 2: "aes", 3: "chacha20"}


class ArchiveError(Exception):
    pass


@dataclass(frozen=True)
class PamtEntry:
    path: str                 # virtual path inside the package, '/' separated
    package: str              # package directory name, e.g. "0004"
    pamt_path: str            # absolute path of the index file
    paz_path: str             # absolute path of the archive holding the data
    paz_index: int
    offset: int
    comp_size: int
    orig_size: int
    flags: int                # the 16-bit flag word (compression | encryption << 4)

    @property
    def basename(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def extension(self) -> str:
        base = self.basename
        dot = base.rfind(".")
        return base[dot:].lower() if dot >= 0 else ""

    @property
    def compression_type(self) -> int:
        return self.flags & 0x0F

    @property
    def encryption_type(self) -> int:
        return (self.flags >> 4) & 0x0F

    @property
    def encrypted(self) -> bool:
        return self.encryption_type != 0

    @property
    def compressed(self) -> bool:
        return self.compression_type != 0 and self.comp_size != self.orig_size

    @property
    def virtual_path(self) -> str:
        """Globally unique display path: ``<package>/<path>``."""

        return f"{self.package}/{self.path}"

    def identity(self) -> Tuple:
        return (self.package, self.path, self.paz_index, self.offset, self.comp_size, self.orig_size, self.flags)


@dataclass
class PamtIndex:
    pamt_path: str
    package: str
    layout: str
    paz_count: int
    header_crc: int
    entries: List[PamtEntry] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


class _NameBlock:
    """Resolves the parent-linked name fragments used by PAMT path tables."""

    def __init__(self, block: bytes) -> None:
        self.block = block
        self.cache: Dict[int, str] = {0xFFFFFFFF: ""}

    def resolve(self, offset: int) -> str:
        if offset == 0xFFFFFFFF or offset >= len(self.block):
            return ""
        cached = self.cache.get(offset)
        if cached is not None:
            return cached
        parts: List[Tuple[int, str]] = []
        cur = offset
        base = ""
        seen = set()
        while cur != 0xFFFFFFFF and len(parts) < 256:
            if cur in seen:
                break
            seen.add(cur)
            hit = self.cache.get(cur)
            if hit is not None:
                base = hit
                break
            if cur + 5 > len(self.block):
                break
            parent = struct.unpack_from("<I", self.block, cur)[0]
            length = self.block[cur + 4]
            if cur + 5 + length > len(self.block):
                break
            parts.append((cur, self.block[cur + 5 : cur + 5 + length].decode("utf-8", errors="replace")))
            cur = parent
        built = base
        for part_offset, part in reversed(parts):
            built = built + part
            self.cache[part_offset] = built
        return self.cache.get(offset, built)


def _package_name(pamt_path: Path) -> str:
    return pamt_path.parent.name


def parse_pamt_bytes(data: bytes, pamt_path: Path) -> PamtIndex:
    """Parse a PAMT index. Tries the current layout first, then the early one."""

    errors: List[str] = []
    for parser in (_parse_layout_v2, _parse_layout_v1):
        try:
            return parser(data, pamt_path)
        except (ArchiveError, struct.error, IndexError) as exc:
            errors.append(f"{parser.__name__}: {exc}")
    raise ArchiveError(f"{pamt_path}: no known PAMT layout matched ({'; '.join(errors)})")


def _parse_layout_v2(data: bytes, pamt_path: Path) -> PamtIndex:
    """Layout used by current game builds (as documented by CDMW)."""

    size = len(data)
    if size < 12:
        raise ArchiveError("file too small")
    header_crc, paz_count, _unknown = struct.unpack_from("<III", data, 0)
    if paz_count == 0 or paz_count > 4096:
        raise ArchiveError(f"implausible paz count {paz_count}")
    off = 12 + paz_count * 12

    def block() -> bytes:
        nonlocal off
        if off + 4 > size:
            raise ArchiveError("truncated block length")
        length = struct.unpack_from("<I", data, off)[0]
        off += 4
        if off + length > size:
            raise ArchiveError("truncated block")
        out = data[off : off + length]
        off += length
        return out

    dir_block = block()
    name_block = block()
    if off + 4 > size:
        raise ArchiveError("truncated folder count")
    folder_count = struct.unpack_from("<I", data, off)[0]
    off += 4
    if off + folder_count * 16 > size:
        raise ArchiveError("truncated folder table")
    folders = [struct.unpack_from("<IIII", data, off + i * 16) for i in range(folder_count)]
    off += folder_count * 16
    if off + 4 > size:
        raise ArchiveError("truncated file count")
    file_count = struct.unpack_from("<I", data, off)[0]
    off += 4
    if off + file_count * 20 != size and off + file_count * 20 > size:
        raise ArchiveError("truncated file table")

    dirs = _NameBlock(dir_block)
    names = _NameBlock(name_block)
    ranges = sorted(
        (start, start + count, dirs.resolve(name_off).replace("\\", "/").strip("/"))
        for _h, name_off, start, count in folders
        if count > 0
    )
    base = pamt_path.parent
    package = _package_name(pamt_path)
    index = PamtIndex(str(pamt_path), package, "v2", paz_count, header_crc)
    trailing = size - (off + file_count * 20)
    if trailing:
        index.warnings.append(f"{trailing} trailing bytes after file table")
    cursor = 0
    for i in range(file_count):
        name_off, paz_off, comp, orig, paz_index, flags = struct.unpack_from("<IIIIHH", data, off + i * 20)
        while cursor < len(ranges) and i >= ranges[cursor][1]:
            cursor += 1
        folder = ""
        if cursor < len(ranges) and ranges[cursor][0] <= i < ranges[cursor][1]:
            folder = ranges[cursor][2]
        rel = names.resolve(name_off).replace("\\", "/").strip("/")
        path = f"{folder}/{rel}" if folder else rel
        if paz_index >= paz_count:
            raise ArchiveError(f"entry {i} references paz {paz_index} of {paz_count}")
        index.entries.append(
            PamtEntry(
                path=path,
                package=package,
                pamt_path=str(pamt_path),
                paz_path=str(base / f"{paz_index}.paz"),
                paz_index=paz_index,
                offset=paz_off,
                comp_size=comp,
                orig_size=orig,
                flags=flags,
            )
        )
    return index


def _parse_layout_v1(data: bytes, pamt_path: Path) -> PamtIndex:
    """Early layout (as documented by crimson-desert-unpacker)."""

    size = len(data)
    off = 4
    paz_count = struct.unpack_from("<I", data, off)[0]
    off += 4 + 8
    if paz_count == 0 or paz_count > 4096:
        raise ArchiveError(f"implausible paz count {paz_count}")
    for i in range(paz_count):
        off += 8
        if i < paz_count - 1:
            off += 4
    folder_size = struct.unpack_from("<I", data, off)[0]
    off += 4
    folder_end = off + folder_size
    prefix = ""
    while off < folder_end:
        parent = struct.unpack_from("<I", data, off)[0]
        length = data[off + 4]
        name = data[off + 5 : off + 5 + length].decode("utf-8", errors="replace")
        if parent == 0xFFFFFFFF:
            prefix = name
        off += 5 + length
    node_size = struct.unpack_from("<I", data, off)[0]
    off += 4
    names = _NameBlock(data[off : off + node_size])
    off += node_size
    folder_count = struct.unpack_from("<I", data, off)[0]
    off += 8 + folder_count * 16
    if off > size or (size - off) % 20:
        raise ArchiveError("file table is not a whole number of 20-byte records")
    base = pamt_path.parent
    package = _package_name(pamt_path)
    stem = int(pamt_path.stem) if pamt_path.stem.isdigit() else 0
    index = PamtIndex(str(pamt_path), package, "v1", paz_count, 0)
    while off + 20 <= size:
        node_ref, paz_off, comp, orig, flags32 = struct.unpack_from("<IIIII", data, off)
        off += 20
        paz_index = flags32 & 0xFF
        rel = names.resolve(node_ref)
        path = f"{prefix}/{rel}".strip("/") if prefix else rel
        index.entries.append(
            PamtEntry(
                path=path.replace("\\", "/"),
                package=package,
                pamt_path=str(pamt_path),
                paz_path=str(base / f"{stem + paz_index}.paz"),
                paz_index=paz_index,
                offset=paz_off,
                comp_size=comp,
                orig_size=orig,
                flags=(flags32 >> 16) & 0xFFFF,
            )
        )
    return index


def parse_pamt(pamt_path: Path) -> PamtIndex:
    with open(pamt_path, "rb") as handle:  # read-only
        data = handle.read()
    return parse_pamt_bytes(data, Path(pamt_path))


def parse_papgt(data: bytes) -> List[Tuple[str, int, int]]:
    """Mount list: ``[(directory_name, flags, pamt_checksum), ...]`` in priority order."""

    header = 12
    record = 12
    for count in range(0, (len(data) - header) // record + 1):
        size_at = header + count * record
        if size_at + 4 > len(data):
            break
        table_size = struct.unpack_from("<I", data, size_at)[0]
        if size_at + 4 + table_size != len(data):
            continue
        table = data[size_at + 4 :]
        out = []
        for i in range(count):
            flags, name_off, checksum = struct.unpack_from("<III", data, header + i * record)
            end = table.find(b"\x00", name_off)
            if name_off >= len(table) or end < 0:
                raise ArchiveError("PAPGT name offset out of range")
            out.append((table[name_off:end].decode("ascii", errors="replace"), flags, checksum))
        return out
    raise ArchiveError("PAPGT string table does not match file size")


# --------------------------------------------------------------------- crypto


def derive_chacha20_key_nonce(filename: str) -> Tuple[bytes, bytes]:
    base = os.path.basename(filename.replace("\\", "/")).lower().encode("utf-8", errors="replace")
    seed = hashlittle(base, CHACHA20_HASH_INITVAL)
    nonce = struct.pack("<I", seed) * 4
    key_base = seed ^ CHACHA20_KEY_XOR
    key = b"".join(struct.pack("<I", key_base ^ d) for d in CHACHA20_XOR_DELTAS)
    return key, nonce


def chacha20_xor(data: bytes, filename: str) -> bytes:
    if Cipher is None:
        raise ArchiveError("the 'cryptography' package is required for encrypted entries")
    key, nonce = derive_chacha20_key_nonce(filename)
    return Cipher(algorithms.ChaCha20(key, nonce), mode=None).encryptor().update(data)


def decompress(entry: PamtEntry, payload: bytes) -> bytes:
    ctype = entry.compression_type
    if ctype == 0 or entry.comp_size == entry.orig_size:
        return payload
    if ctype == 2:
        if _lz4_block is None:
            raise ArchiveError("the 'lz4' package is required for LZ4 entries")
        return _lz4_block.decompress(payload, uncompressed_size=entry.orig_size)
    if ctype in (3, 4):
        try:
            return zlib.decompress(payload)
        except zlib.error as exc:
            raise ArchiveError(f"compression type {ctype} is not zlib ({exc})") from exc
    raise ArchiveError(f"unsupported compression type {ctype} ({COMPRESSION_LABELS.get(ctype, '?')})")


class ArchiveReader:
    """Reads entry payloads. Handles are opened read-only and cached."""

    def __init__(self, max_handles: int = 16) -> None:
        self._handles: Dict[str, BinaryIO] = {}
        self._max = max_handles

    def _handle(self, path: str) -> BinaryIO:
        handle = self._handles.get(path)
        if handle is None:
            if len(self._handles) >= self._max:
                _, old = self._handles.popitem()
                old.close()
            handle = open(path, "rb")  # noqa: SIM115 - intentionally cached, read-only
            self._handles[path] = handle
        return handle

    def read_raw(self, entry: PamtEntry, limit: Optional[int] = None) -> bytes:
        size = entry.comp_size if limit is None else min(limit, entry.comp_size)
        handle = self._handle(entry.paz_path)
        handle.seek(entry.offset)
        data = handle.read(size)
        if len(data) != size:
            raise ArchiveError(f"{entry.virtual_path}: short read ({len(data)} of {size} bytes)")
        return data

    def read(self, entry: PamtEntry, limit: Optional[int] = None) -> bytes:
        """Return decrypted, decompressed bytes.

        ``limit`` requests only a prefix. It is honoured only when the entry is
        stored uncompressed (ChaCha20 is a stream cipher, so a prefix decrypts
        correctly); compressed entries are always read whole.
        """

        partial = limit is not None and not entry.compressed and limit < entry.comp_size
        raw = self.read_raw(entry, limit if partial else None)
        if entry.encryption_type == 3:
            raw = chacha20_xor(raw, entry.basename)
        elif entry.encryption_type != 0:
            raise ArchiveError(
                f"{entry.virtual_path}: unsupported encryption {ENCRYPTION_LABELS.get(entry.encryption_type, entry.encryption_type)}"
            )
        if partial:
            return raw
        return decompress(entry, raw)

    def close(self) -> None:
        for handle in self._handles.values():
            handle.close()
        self._handles.clear()

    def __enter__(self) -> "ArchiveReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ------------------------------------------------------------ test support


def build_pamt_v2(files: List[Tuple[str, int, int, int, int, int]], paz_sizes: List[int]) -> bytes:
    """Serialize a minimal v2 PAMT. Used by tests and fixture generation only.

    ``files``: ``(path, paz_index, offset, comp_size, orig_size, flags)``.
    """

    def name_block(paths: List[str]) -> Tuple[bytes, Dict[str, int]]:
        blob = bytearray()
        offsets: Dict[str, int] = {}
        for p in paths:
            offsets[p] = len(blob)
            raw = p.encode("utf-8")
            blob += struct.pack("<IB", 0xFFFFFFFF, len(raw)) + raw
        return bytes(blob), offsets

    by_folder: Dict[str, List[Tuple]] = {}
    for item in files:
        folder, _, name = item[0].rpartition("/")
        by_folder.setdefault(folder, []).append((name,) + item[1:])
    folders = sorted(by_folder)
    dir_blob, dir_offsets = name_block(folders)
    names = [f[0] for folder in folders for f in by_folder[folder]]
    name_blob, name_offsets = name_block(sorted(set(names)))
    out = bytearray(struct.pack("<III", 0, len(paz_sizes), 0))
    for i, s in enumerate(paz_sizes):
        out += struct.pack("<III", 0, s, 0)
    out += struct.pack("<I", len(dir_blob)) + dir_blob
    out += struct.pack("<I", len(name_blob)) + name_blob
    out += struct.pack("<I", len(folders))
    start = 0
    for folder in folders:
        out += struct.pack("<IIII", 0, dir_offsets[folder], start, len(by_folder[folder]))
        start += len(by_folder[folder])
    out += struct.pack("<I", start)
    for folder in folders:
        for name, paz_index, offset, comp, orig, flags in by_folder[folder]:
            out += struct.pack("<IIIIHH", name_offsets[name], offset, comp, orig, paz_index, flags)
    return bytes(out)

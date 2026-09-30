"""Wwise ``.wem`` (RIFF/RIFX WAVE) header analysis.

Only headers are read - no decoding. Duration is reported together with how
it was obtained so later layers can weigh it:

* ``exact_pcm``         - PCM data size / block align.
* ``fmt_sample_count``  - sample count stored in the Wwise ``fmt`` extension
                          (Vorbis/Opus), cross-checked against the byte rate.
* ``byte_rate_estimate``- data size / average bytes per second.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional

CODECS = {
    0x0001: "PCM",
    0x0002: "Wwise IMA ADPCM",
    0x0069: "IMA ADPCM",
    0x0161: "xWMA",
    0x0162: "xWMA",
    0x0165: "XMA2",
    0x0166: "XMA2",
    0xAAC0: "AAC",
    0xFFF0: "DSP ADPCM",
    0xFFFB: "HEVAG",
    0xFFFC: "ATRAC9",
    0xFFFE: "PCM (extensible)",
    0xFFFF: "Wwise Vorbis",
    0x3039: "Opus (NX)",
    0x3040: "Opus",
    0x3041: "Wwise Opus",
    0x8311: "PTADPCM",
}


@dataclass
class WemInfo:
    valid: bool = False
    big_endian: bool = False
    riff_size: int = 0
    total_size: int = 0
    format_tag: Optional[int] = None
    codec: str = "unknown"
    channels: int = 0
    sample_rate: int = 0
    avg_bytes_per_sec: int = 0
    block_align: int = 0
    bits_per_sample: int = 0
    fmt_size: int = 0
    channel_mask: Optional[int] = None
    data_offset: Optional[int] = None
    data_size: Optional[int] = None
    sample_count: Optional[int] = None
    duration_seconds: Optional[float] = None
    duration_method: Optional[str] = None
    loops: List[Dict[str, int]] = field(default_factory=list)
    cue_points: List[Dict[str, int]] = field(default_factory=list)
    labels: Dict[int, str] = field(default_factory=dict)
    chunks: List[str] = field(default_factory=list)
    unknown_chunks: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    truncated_header: bool = False

    def to_dict(self) -> Dict[str, object]:
        return {
            "valid": self.valid,
            "codec": self.codec,
            "format_tag": None if self.format_tag is None else f"0x{self.format_tag:04X}",
            "channels": self.channels,
            "sample_rate": self.sample_rate,
            "avg_bytes_per_sec": self.avg_bytes_per_sec,
            "block_align": self.block_align,
            "bits_per_sample": self.bits_per_sample,
            "channel_mask": self.channel_mask,
            "data_size": self.data_size,
            "sample_count": self.sample_count,
            "duration_seconds": self.duration_seconds,
            "duration_method": self.duration_method,
            "loops": self.loops,
            "cue_points": self.cue_points,
            "labels": {str(k): v for k, v in self.labels.items()},
            "chunks": self.chunks,
            "unknown_chunks": self.unknown_chunks,
            "warnings": self.warnings,
        }


KNOWN_CHUNKS = {"fmt ", "data", "smpl", "cue ", "LIST", "JUNK", "akd ", "hash", "vorb", "seek", "XMA2", "meta", "labl", "adtl"}


def parse_wem(data: bytes, total_size: Optional[int] = None) -> WemInfo:
    """Parse a WEM header. ``data`` may be only a prefix of the file."""

    info = WemInfo(total_size=total_size if total_size is not None else len(data))
    if len(data) < 12 or data[8:12] != b"WAVE" or data[:4] not in (b"RIFF", b"RIFX"):
        info.warnings.append("not a RIFF/RIFX WAVE file")
        return info
    info.big_endian = data[:4] == b"RIFX"
    e = ">" if info.big_endian else "<"
    info.riff_size = struct.unpack_from(e + "I", data, 4)[0]
    off = 12
    fmt_off = None
    while off + 8 <= len(data):
        tag = data[off : off + 4].decode("ascii", errors="replace")
        size = struct.unpack_from(e + "I", data, off + 4)[0]
        body = off + 8
        info.chunks.append(tag)
        if tag not in KNOWN_CHUNKS:
            info.unknown_chunks.append(tag)
        if tag == "fmt ":
            fmt_off = body
            info.fmt_size = size
            if body + 16 <= len(data):
                (info.format_tag, info.channels, info.sample_rate, info.avg_bytes_per_sec,
                 info.block_align, info.bits_per_sample) = struct.unpack_from(e + "HHIIHH", data, body)
                info.codec = CODECS.get(info.format_tag, f"unknown (0x{info.format_tag:04X})")
                if size >= 0x18 and body + 0x18 <= len(data):
                    info.channel_mask = struct.unpack_from(e + "I", data, body + 0x14)[0]
        elif tag == "data":
            info.data_offset = body
            info.data_size = size
            break  # everything after data is audio; stop walking
        elif tag == "smpl" and body + 0x24 <= len(data):
            count = struct.unpack_from(e + "I", data, body + 0x1C)[0]
            for i in range(min(count, 64)):
                p = body + 0x24 + i * 24
                if p + 24 > len(data):
                    break
                cue_id, loop_type, start, end, _frac, play_count = struct.unpack_from(e + "IIIIII", data, p)
                info.loops.append({"id": cue_id, "type": loop_type, "start_sample": start, "end_sample": end, "play_count": play_count})
        elif tag == "cue " and body + 4 <= len(data):
            count = struct.unpack_from(e + "I", data, body)[0]
            for i in range(min(count, 256)):
                p = body + 4 + i * 24
                if p + 24 > len(data):
                    break
                cue_id, position = struct.unpack_from(e + "II", data, p)
                sample_offset = struct.unpack_from(e + "I", data, p + 20)[0]
                info.cue_points.append({"id": cue_id, "position": position, "sample_offset": sample_offset})
        elif tag == "LIST" and body + 4 <= len(data) and data[body : body + 4] == b"adtl":
            p = body + 4
            stop = min(len(data), body + size)
            while p + 8 <= stop:
                sub = data[p : p + 4]
                sub_size = struct.unpack_from(e + "I", data, p + 4)[0]
                if sub == b"labl" and p + 12 <= stop:
                    cue_id = struct.unpack_from(e + "I", data, p + 8)[0]
                    text = data[p + 12 : p + 8 + sub_size].split(b"\x00", 1)[0]
                    info.labels[cue_id] = text.decode("utf-8", errors="replace")
                p += 8 + sub_size + (sub_size & 1)
        nxt = body + size
        if size & 1 and not _looks_like_tag(data, nxt) and _looks_like_tag(data, nxt + 1):
            nxt += 1  # standard RIFF word padding
        off = nxt
    if info.format_tag is None:
        info.warnings.append("no fmt chunk found in the bytes available")
        info.truncated_header = len(data) < info.total_size
        return info
    info.valid = True
    _derive_duration(info, data, fmt_off, e)
    return info


def _looks_like_tag(data: bytes, off: int) -> bool:
    if off + 4 > len(data):
        return False
    return all(0x20 <= b <= 0x7E for b in data[off : off + 4])


def _derive_duration(info: WemInfo, data: bytes, fmt_off: Optional[int], e: str) -> None:
    if info.data_size is None:
        # data chunk beyond the prefix we have: estimate from total size
        info.warnings.append("data chunk not inside the bytes read")
        data_size = max(0, info.total_size - 64)
    else:
        data_size = info.data_size
    sr = info.sample_rate
    estimate = data_size / info.avg_bytes_per_sec if info.avg_bytes_per_sec else None
    tag = info.format_tag
    if tag in (0x0001, 0xFFFE) and info.block_align:
        info.sample_count = data_size // info.block_align
        info.duration_method = "exact_pcm"
    elif tag == 0x0002 and info.block_align and info.channels:
        per_block = (info.block_align // info.channels - 4) * 2 + 1
        info.sample_count = (data_size // info.block_align) * per_block
        info.duration_method = "ima_blocks"
    elif tag in (0xFFFF, 0x3040, 0x3041, 0x3039) and fmt_off is not None and info.fmt_size >= 0x1C and fmt_off + 0x1C <= len(data):
        candidate = struct.unpack_from(e + "I", data, fmt_off + 0x18)[0]
        if sr and 0 < candidate < sr * 6 * 3600:
            seconds = candidate / sr
            if estimate is None or (estimate / 3.0) <= seconds <= (estimate * 3.0 + 1.0):
                info.sample_count = candidate
                info.duration_method = "fmt_sample_count"
            else:
                info.warnings.append(
                    f"fmt sample count {candidate} ({seconds:.1f}s) disagrees with byte-rate estimate {estimate:.1f}s"
                )
    if info.sample_count is not None and sr:
        info.duration_seconds = round(info.sample_count / sr, 3)
    elif estimate is not None:
        info.duration_seconds = round(estimate, 3)
        info.duration_method = "byte_rate_estimate"

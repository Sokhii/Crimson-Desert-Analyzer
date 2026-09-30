"""Synthetic Wwise / Crimson Desert data builders.

Used by the automated test-suite and by ``tools/make_fake_install.py`` so the
whole pipeline can be exercised without proprietary game files. The layouts
written here follow the same format description as the parsers (bank version
150 by default).
"""

from __future__ import annotations

import os
import struct
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from cstudio.formats.hashing import wwise_fnv1_32
from cstudio.formats.paz import build_pamt_v2, chacha20_xor


def u8(v: int) -> bytes:
    return struct.pack("<B", v)


def u16(v: int) -> bytes:
    return struct.pack("<H", v)


def u32(v: int) -> bytes:
    return struct.pack("<I", v & 0xFFFFFFFF)


def s32(v: int) -> bytes:
    return struct.pack("<i", v)


def f32(v: float) -> bytes:
    return struct.pack("<f", v)


def f64(v: float) -> bytes:
    return struct.pack("<d", v)


def chunk(tag: bytes, payload: bytes) -> bytes:
    return tag + u32(len(payload)) + payload


# ------------------------------------------------------------------ WEM


def make_wem(
    *,
    format_tag: int = 0xFFFF,
    channels: int = 2,
    sample_rate: int = 48000,
    seconds: float = 120.0,
    avg_bytes_per_sec: int = 2000,
    loop: Optional[Tuple[int, int]] = None,
    data_bytes: Optional[int] = None,
) -> bytes:
    """A header-accurate WEM with a zero-filled (or truncated) data chunk."""

    sample_count = int(seconds * sample_rate)
    if format_tag in (0x0001, 0xFFFE):
        block_align = channels * 2
        avg_bytes_per_sec = sample_rate * block_align
        fmt = u16(format_tag) + u16(channels) + u32(sample_rate) + u32(avg_bytes_per_sec) + u16(block_align) + u16(16)
        payload_size = sample_count * block_align
    else:
        block_align = 0
        fmt = u16(format_tag) + u16(channels) + u32(sample_rate) + u32(avg_bytes_per_sec) + u16(block_align) + u16(0)
        fmt += u16(0x30) + u16(0) + u32(3 if channels == 2 else 4)  # cbSize, samples/block, channel mask
        fmt += u32(sample_count)  # 0x18: sample count
        fmt += b"\x00" * (0x42 - len(fmt))
        payload_size = int(seconds * avg_bytes_per_sec)
    body = chunk(b"fmt ", fmt)
    if loop:
        smpl = u32(0) * 7 + u32(1) + u32(0) + u32(0) + u32(0) + u32(loop[0]) + u32(loop[1]) + u32(0) + u32(0)
        body += chunk(b"smpl", smpl)
    body += chunk(b"akd ", b"\x00" * 16)
    size = payload_size if data_bytes is None else data_bytes
    body += b"data" + u32(payload_size) + b"\x00" * size
    return b"RIFF" + u32(4 + len(body)) + b"WAVE" + body


# ------------------------------------------------------------------ BNK


class BankBuilder:
    """Builds a version-150 soundbank with a small hierarchy."""

    def __init__(self, name: str, version: int = 150) -> None:
        self.name = name
        self.version = version
        self.bank_id = wwise_fnv1_32(name)
        self.objects: List[bytes] = []
        self.media: List[Tuple[int, bytes]] = []

    # ---- helpers shared by node types
    def _node_base(self, parent: int = 0, bus: int = 0, loop: Optional[int] = None) -> bytes:
        v = self.version
        out = u8(0) + u8(0)  # fx override, fx count
        if v > 136:
            out += u8(0) + u8(0)  # metadata override, count
        if 89 < v <= 145:
            out += u8(0)
        out += u32(bus) + u32(parent) + u8(0)
        if loop is not None:
            out += u8(1) + u8(0x3A if v <= 145 else 0x4A) + u32(loop)
        else:
            out += u8(0)
        out += u8(0)  # ranged props
        out += u8(0)  # positioning: no override
        out += u8(0)  # aux bits
        if v > 135:
            out += u32(0)
        out += b"\x00" * 6  # adv settings
        out += u8(0) + u8(0)  # state props (var 0), state groups (var 0)
        out += u16(0)  # rtpc
        return out

    def _obj(self, type_code: int, object_id: int, body: bytes) -> None:
        payload = u32(object_id) + body
        self.objects.append(u8(type_code) + u32(len(payload)) + payload)

    def _source(self, source_id: int, stream_type: int, size: int, plugin: int = 0x00040001) -> bytes:
        out = u32(plugin) + u8(stream_type) + u32(source_id)
        if self.version > 150:
            out += u32(0)
        return out + u32(size) + u8(0)

    # ---- object types
    def sound(self, object_id: int, source_id: int, parent: int = 0, streaming: bool = True, loop: Optional[int] = None) -> int:
        body = self._source(source_id, 2 if streaming else 0, 0 if streaming else 1024) + self._node_base(parent, loop=loop)
        self._obj(0x02, object_id, body)
        return object_id

    def embed(self, source_id: int, wem: bytes) -> None:
        self.media.append((source_id, wem))

    def action_play(self, object_id: int, target: int) -> int:
        body = u16(0x0403) + u32(target) + u8(0) + u8(0) + u8(0) + u8(4) + u32(self.bank_id)
        if self.version >= 144:
            body += u32(0)
        self._obj(0x03, object_id, body)
        return object_id

    def action_set_state(self, object_id: int, group: int, state: int) -> int:
        body = u16(0x1204) + u32(0) + u8(0) + u8(0) + u8(0) + u32(group) + u32(state)
        self._obj(0x03, object_id, body)
        return object_id

    def event(self, name_or_id, actions: Sequence[int]) -> int:
        event_id = wwise_fnv1_32(name_or_id) if isinstance(name_or_id, str) else name_or_id
        body = b""
        if self.version > 154:
            body += u8(0) + u16(0) + f32(0)
        body += u8(len(actions)) + b"".join(u32(a) for a in actions)
        self._obj(0x04, event_id, body)
        return event_id

    def ranseq(self, object_id: int, children: Sequence[int], parent: int = 0) -> int:
        body = self._node_base(parent)
        body += u16(1) + u16(0) + u16(0) + f32(1000.0) + f32(0) + f32(0) + u16(0)
        body += u8(0) + u8(0) + u8(1) + u8(0x08)
        body += u32(len(children)) + b"".join(u32(c) for c in children)
        body += u16(len(children)) + b"".join(u32(c) + s32(50000) for c in children)
        self._obj(0x05, object_id, body)
        return object_id

    def actor_mixer(self, object_id: int, children: Sequence[int], parent: int = 0) -> int:
        body = self._node_base(parent) + u32(len(children)) + b"".join(u32(c) for c in children)
        self._obj(0x07, object_id, body)
        return object_id

    def _music_node(self, children: Sequence[int], parent: int, tempo: float = 120.0) -> bytes:
        return (
            u8(0)
            + self._node_base(parent)
            + u32(len(children))
            + b"".join(u32(c) for c in children)
            + f64(2000.0) + f64(0.0) + f32(tempo) + u8(4) + u8(4) + u8(1)
            + u32(0)
        )

    def music_track(self, object_id: int, source_ids: Sequence[int], parent: int, durations_ms: Sequence[float],
                    streaming: bool = True) -> int:
        v = self.version
        body = b""
        if 89 < v <= 152:
            body += u8(0)
        body += u32(len(source_ids)) + b"".join(self._source(s, 2 if streaming else 0, 0) for s in source_ids)
        if v > 152:
            body += u8(0)
        body += u32(len(source_ids))
        for i, (sid, dur) in enumerate(zip(source_ids, durations_ms)):
            body += u32(i) + u32(sid)
            if v > 150:
                body += u32(0)
            if v > 132:
                body += u32(0)
            body += f64(0.0) + f64(0.0) + f64(0.0) + f64(dur)
        if source_ids:
            body += u32(1)
        body += u32(0)  # clip automation
        body += self._node_base(parent)
        body += u8(0)  # track type normal
        body += s32(0)
        self._obj(0x0B, object_id, body)
        return object_id

    def music_segment(self, object_id: int, tracks: Sequence[int], parent: int, duration_ms: float,
                      markers: Iterable[Tuple[int, float, str]] = ()) -> int:
        body = self._music_node(tracks, parent) + f64(duration_ms)
        markers = list(markers)
        body += u32(len(markers))
        for mid, pos, name in markers:
            body += u32(mid) + f64(pos)
            if self.version <= 136:
                raw = name.encode()
                body += u32(len(raw)) + raw
            else:
                body += name.encode() + b"\x00"
        self._obj(0x0A, object_id, body)
        return object_id

    def _trans_node(self, children: Sequence[int], parent: int, transition_segment: Optional[int] = None) -> bytes:
        out = self._music_node(children, parent)
        out += u32(1)  # one rule
        out += u32(1) + u32(0xFFFFFFFF) + u32(1) + u32(0xFFFFFFFF)
        out += s32(500) + u32(4) + s32(0) + u32(0) + u32(0) + u8(0)
        out += s32(500) + u32(4) + s32(0) + u32(0) + u32(0)
        if self.version > 132:
            out += u16(0)
        out += u16(0) + u8(0) + u8(0)
        if transition_segment is not None:
            out += u8(1) + u32(transition_segment) + b"\x00" * 24 + u8(0) + u8(0)
        else:
            out += u8(0)
        return out

    def music_ranseq(self, object_id: int, segments: Sequence[int], parent: int = 0) -> int:
        body = self._trans_node(segments, parent)
        body += u32(1 + len(segments))
        # root node
        body += u32(0) + u32(object_id + 1) + u32(len(segments)) + u32(0) + struct.pack("<hhh", 1, 0, 0) + u32(50000) + u16(0) + u8(0) + u8(0)
        for i, seg in enumerate(segments):
            body += u32(seg) + u32(object_id + 2 + i) + u32(0) + u32(0xFFFFFFFF) + struct.pack("<hhh", 1, 0, 0) + u32(50000) + u16(0) + u8(0) + u8(0)
        self._obj(0x0D, object_id, body)
        return object_id

    def music_switch(self, object_id: int, group_id: int, mapping: Dict[int, int], parent: int = 0,
                     transition_segment: Optional[int] = None) -> int:
        children = list(mapping.values())
        body = self._trans_node(children, parent, transition_segment)
        body += u8(0) + u32(1) + u32(group_id) + u8(1)
        tree = u32(0) + u16(1) + u16(len(mapping)) + u16(50) + u16(100)
        for state, node in mapping.items():
            tree += u32(state) + u32(node) + u16(50) + u16(100)
        body += u32(len(tree)) + u8(0) + tree
        self._obj(0x0C, object_id, body)
        return object_id

    def raw_object(self, type_code: int, object_id: int, body: bytes) -> None:
        self._obj(type_code, object_id, body)

    def build(self, bank_names: Optional[Dict[int, str]] = None) -> bytes:
        v = self.version
        bkhd = u32(v) + u32(self.bank_id) + u32(wwise_fnv1_32("sfx")) + u32(0x10) + u32(1234)
        if v > 141:
            bkhd += u32(0) + b"\x00" * 16
        out = chunk(b"BKHD", bkhd)
        if self.media:
            didx = b""
            data = b""
            for sid, wem in self.media:
                while len(data) % 16:
                    data += b"\x00"
                didx += u32(sid) + u32(len(data)) + u32(len(wem))
                data += wem
            out += chunk(b"DIDX", didx) + chunk(b"DATA", data)
        out += chunk(b"HIRC", u32(len(self.objects)) + b"".join(self.objects))
        names = bank_names if bank_names is not None else {self.bank_id: self.name}
        stid = u32(1) + u32(len(names))
        for bid, name in names.items():
            raw = name.encode()
            stid += u32(bid) + u8(len(raw)) + raw
        out += chunk(b"STID", stid)
        return out


# ------------------------------------------------------- soundbanksinfo


def make_soundbanksinfo(banks: Dict[str, dict], streamed: Dict[int, str]) -> bytes:
    """banks: name -> {"events": [names], "memory": {id: shortname}, "streamed_refs": [ids]}"""

    lines = ['<?xml version="1.0" encoding="utf-8"?>',
             '<SoundBanksInfo Platform="Windows" BasePlatform="Windows" SchemaVersion="16" SoundbankVersion="150">',
             "  <StreamedFiles>"]
    for sid, short in streamed.items():
        lines.append(f'    <File Id="{sid}" Language="SFX"><ShortName>{short}</ShortName><Path>{sid}.wem</Path></File>')
    lines.append("  </StreamedFiles>")
    lines.append("  <SoundBanks>")
    for name, spec in banks.items():
        bid = wwise_fnv1_32(name)
        lines.append(f'    <SoundBank Id="{bid}" Type="User" Language="SFX">')
        lines.append(f"      <ObjectPath>\\SoundBanks\\Default Work Unit\\{name}</ObjectPath>")
        lines.append(f"      <ShortName>{name}</ShortName><Path>{name}.bnk</Path>")
        lines.append("      <IncludedEvents>")
        for ev in spec.get("events", []):
            lines.append(f'        <Event Id="{wwise_fnv1_32(ev)}" Name="{ev}" ObjectPath="\\Events\\{ev}"/>')
        lines.append("      </IncludedEvents>")
        if spec.get("memory"):
            lines.append("      <IncludedMemoryFiles>")
            for sid, short in spec["memory"].items():
                lines.append(f'        <File Id="{sid}" Language="SFX"><ShortName>{short}</ShortName><Path>SFX\\{short}</Path></File>')
            lines.append("      </IncludedMemoryFiles>")
        if spec.get("streamed_refs"):
            lines.append("      <ReferencedStreamedFiles>")
            for sid in spec["streamed_refs"]:
                lines.append(f'        <File Id="{sid}"/>')
            lines.append("      </ReferencedStreamedFiles>")
        lines.append("    </SoundBank>")
    lines.append("  </SoundBanks>")
    lines.append("  <StateGroups><StateGroup Id=\"%d\" Name=\"BGM_Region\"><States><State Id=\"%d\" Name=\"Desert\"/></States></StateGroup></StateGroups>"
                 % (wwise_fnv1_32("BGM_Region"), wwise_fnv1_32("Desert")))
    lines.append("</SoundBanksInfo>")
    return "\n".join(lines).encode("utf-8")


# ------------------------------------------------------- fake install


def write_package(package_dir: Path, files: Dict[str, bytes], encrypt: Iterable[str] = (), compress: Iterable[str] = ()) -> None:
    """Write ``0.pamt`` + ``0.paz`` holding ``files`` (virtual path -> bytes)."""

    import lz4.block

    encrypt = set(encrypt)
    compress = set(compress)
    package_dir.mkdir(parents=True, exist_ok=True)
    paz = bytearray()
    records = []
    for path, content in files.items():
        flags = 0
        stored = content
        if path in compress:
            stored = lz4.block.compress(content, store_size=False)
            flags |= 2
        if path in encrypt:
            stored = chacha20_xor(stored, os.path.basename(path))
            flags |= 3 << 4
        while len(paz) % 16:
            paz.append(0)
        records.append((path, 0, len(paz), len(stored), len(content), flags))
        paz += stored
    (package_dir / "0.paz").write_bytes(bytes(paz))
    (package_dir / "0.pamt").write_bytes(build_pamt_v2(records, [len(paz)]))


def make_fake_install(root: Path) -> Dict[str, object]:
    """Create a miniature Crimson Desert-like installation for tests/demos."""

    root.mkdir(parents=True, exist_ok=True)
    (root / "bin64").mkdir(exist_ok=True)
    (root / "bin64" / "CrimsonDesert.exe").write_bytes(b"MZ fake")

    music = BankBuilder("bgm")
    region_group = wwise_fnv1_32("BGM_Region")
    desert = wwise_fnv1_32("Desert")
    forest = wwise_fnv1_32("Forest")
    wem_desert_a, wem_desert_b, wem_forest, wem_stinger = 433831842, 353733717, 480286974, 558103
    t1 = music.music_track(1001, [wem_desert_a], parent=2001, durations_ms=[180000.0])
    t2 = music.music_track(1002, [wem_desert_b], parent=2002, durations_ms=[150000.0])
    t3 = music.music_track(1003, [wem_forest], parent=2003, durations_ms=[200000.0])
    t4 = music.music_track(1004, [wem_stinger], parent=2004, durations_ms=[4000.0], streaming=False)
    s1 = music.music_segment(2001, [t1], parent=3001, duration_ms=180000.0, markers=[(0, 0.0, "Entry"), (1, 178000.0, "Exit")])
    s2 = music.music_segment(2002, [t2], parent=3001, duration_ms=150000.0)
    s3 = music.music_segment(2003, [t3], parent=3002, duration_ms=200000.0)
    s4 = music.music_segment(2004, [t4], parent=4001, duration_ms=4000.0)
    p1 = music.music_ranseq(3001, [s1, s2], parent=4001)
    p2 = music.music_ranseq(3002, [s3], parent=4001)
    music.music_switch(4001, region_group, {desert: p1, forest: p2}, transition_segment=s4)
    a1 = music.action_play(5001, 4001)
    a2 = music.action_set_state(5002, region_group, desert)
    music.event("Play_BGM_World", [a1])
    music.event("Set_BGM_Region_Desert", [a2])
    music.embed(wem_stinger, make_wem(seconds=4.0))
    # an object type the analyzer does not decode, to exercise unknown handling
    music.raw_object(0x2A, 9001, u32(4001) + b"\x01\x02\x03\x04")

    sfx = BankBuilder("env_region_desert")
    wem_wind = 16721128
    snd = sfx.sound(6001, wem_wind, parent=6002, loop=0)
    sfx.ranseq(6002, [snd])
    sfx.event("Play_env_region_desert", [sfx.action_play(6003, 6002)])

    xml = make_soundbanksinfo(
        {
            "bgm": {"events": ["Play_BGM_World", "Set_BGM_Region_Desert"], "memory": {wem_stinger: "bgm_cd_stinger_01.wav"},
                    "streamed_refs": [wem_desert_a, wem_desert_b, wem_forest]},
            "env_region_desert": {"events": ["Play_env_region_desert"], "streamed_refs": [wem_wind]},
        },
        {
            wem_desert_a: "bgm_cd_region_big_desert_1normal_jiyoon_01_v01.wav",
            wem_desert_b: "bgm_cd_region_big_desert_1normal_joo_01_v01.wav",
            wem_forest: "CD_Field_Desert-01.wav",
            wem_wind: "env__region__desert__2d__day__lp.wav",
        },
    )
    files = {
        "sound/bgm.bnk": music.build(),
        "sound/env_region_desert.bnk": sfx.build(),
        "sound/soundbanksinfo.xml": xml,
        f"sound/{wem_desert_a}.wem": make_wem(seconds=180.0, loop=(0, 180 * 48000 - 1)),
        f"sound/{wem_desert_b}.wem": make_wem(seconds=150.0),
        f"sound/{wem_forest}.wem": make_wem(seconds=200.0),
        f"sound/{wem_wind}.wem": make_wem(seconds=30.0, channels=1, format_tag=0x0001),
        "sound/readme_unknown.pasnd": b"PASN\x01\x00\x00\x00unknown format",
        "gamedata/musicinfo.pabgb": b"\x00\x00Play_BGM_World\x00Set_BGM_Region_Desert\x00",
    }
    write_package(root / "0004", files, encrypt={"sound/soundbanksinfo.xml"}, compress={"sound/soundbanksinfo.xml", "sound/bgm.bnk"})
    write_package(root / "0000", {"ui/title.xml": b"<Ui/>"})
    (root / "meta").mkdir(exist_ok=True)
    return {
        "music_wems": [wem_desert_a, wem_desert_b, wem_forest, wem_stinger],
        "ambience_wems": [wem_wind],
        "bank_ids": {"bgm": music.bank_id, "env_region_desert": sfx.bank_id},
    }

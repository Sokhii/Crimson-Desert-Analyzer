"""Wwise SoundBank (``.bnk``) parser.

A bank is a flat list of chunks (``BKHD``, ``DIDX``, ``DATA``, ``HIRC``,
``STID``, ...). ``HIRC`` holds the object hierarchy: sounds, containers,
events, actions and the interactive-music objects (segments, tracks,
switch/playlist containers).

Every HIRC object starts with ``type:u8 size:u32 id:u32`` so the object list
can always be walked, even when an object's body is not understood. Bodies are
decoded field by field for the object types that matter for music. The field
layout follows the publicly documented Wwise bank format (as described by the
``wwiser`` project, used here as a reference only - see
``docs/research/wwise_bank_format.md``). Objects that cannot be decoded are
kept, flagged, and handed to the unknown-structure pipeline instead of being
silently dropped.

Parse status per object:

* ``parsed``      - every byte of the body was consumed by the decoder.
* ``partial``     - the decoder extracted the leading fields (e.g. source or
                    children) but stopped before the end or found leftovers.
* ``header_only`` - type/size/id only (type not decoded or version unsupported).
* ``failed``      - the decoder raised; only the header is trusted.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .binreader import BinaryReader, ParseError

# Versions whose layout the body decoders follow. Outside this window object
# headers are still read, bodies are not trusted.
MIN_DECODED_VERSION = 128
MAX_DECODED_VERSION = 172

HIRC_TYPES_128 = {
    0x01: "State",
    0x02: "Sound",
    0x03: "Action",
    0x04: "Event",
    0x05: "RandomSequenceContainer",
    0x06: "SwitchContainer",
    0x07: "ActorMixer",
    0x08: "Bus",
    0x09: "LayerContainer",
    0x0A: "MusicSegment",
    0x0B: "MusicTrack",
    0x0C: "MusicSwitchContainer",
    0x0D: "MusicRandomSequenceContainer",
    0x0E: "Attenuation",
    0x0F: "DialogueEvent",
    0x10: "FxShareSet",
    0x11: "FxCustom",
    0x12: "AuxBus",
    0x13: "LFOModulator",
    0x14: "EnvelopeModulator",
    0x15: "AudioDevice",
    0x16: "TimeModulator",
    0x17: "SidechainMix",
}

MUSIC_TYPES = {0x0A, 0x0B, 0x0C, 0x0D}
CONTAINER_TYPES = {0x05, 0x06, 0x07, 0x09, 0x0A, 0x0C, 0x0D}

ACTION_TYPES = {
    0x0403: "Play",
    0x0503: "PlayAndContinue",
    0x0102: "Stop_E", 0x0103: "Stop_E_O", 0x0104: "Stop_ALL", 0x0105: "Stop_ALL_O",
    0x0202: "Pause_E", 0x0203: "Pause_E_O", 0x0204: "Pause_ALL",
    0x0302: "Resume_E", 0x0303: "Resume_E_O", 0x0304: "Resume_ALL",
    0x1204: "SetState", 0x1901: "SetSwitch",
    0x1302: "SetGameParameter", 0x1303: "SetGameParameter_O",
    0x1402: "ResetGameParameter", 0x1403: "ResetGameParameter_O",
    0x1511: "Trigger", 0x1D00: "Release", 0x1E02: "Seek_E", 0x1E03: "Seek_E_O",
    0x2103: "PlayEvent", 0x2202: "ResetPlaylist_E", 0x2203: "ResetPlaylist_E_O",
    0x0A02: "SetVolume_M", 0x0A03: "SetVolume_O", 0x0B02: "ResetVolume_M",
    0x0602: "Mute_M", 0x0603: "Mute_O", 0x0702: "Unmute_M", 0x0703: "Unmute_O",
    0x1C02: "Break_E", 0x1C03: "Break_E_O",
}

SOURCE_STREAM_TYPES = {0: "embedded", 1: "prefetch_streaming", 2: "streaming"}
MUSIC_TRACK_TYPES = {0: "normal", 1: "random", 2: "sequence", 3: "switch"}
RANSEQ_MODES = {0: "random", 1: "sequence"}
PLAYLIST_RS_TYPES = {0: "continuous_sequence", 1: "step_sequence", 2: "continuous_random", 3: "step_random"}


def loop_prop_id(version: int) -> int:
    """The AkPropID of the ``Loop`` property (changed between bank versions)."""

    return 0x3A if version <= 145 else 0x4A


@dataclass
class BankChunk:
    tag: str
    offset: int  # payload offset
    size: int


@dataclass
class EmbeddedMedia:
    index: int
    source_id: int
    offset: int  # absolute offset of the media inside the bank file
    size: int


@dataclass
class HircObject:
    type_code: int
    object_id: int
    offset: int  # offset of the object body (after type+size) inside the bank
    size: int
    type_name: str = ""
    parse_status: str = "header_only"
    error: Optional[str] = None
    consumed: int = 0
    fields: Dict[str, object] = field(default_factory=dict)
    refs: List[Tuple[str, int]] = field(default_factory=list)

    @property
    def leftover(self) -> int:
        return max(0, self.size - self.consumed)


@dataclass
class BankInfo:
    version: int = 0
    bank_id: int = 0
    language_id: int = 0
    project_id: int = 0
    bank_type: Optional[int] = None
    header_fields: Dict[str, object] = field(default_factory=dict)
    chunks: List[BankChunk] = field(default_factory=list)
    media: List[EmbeddedMedia] = field(default_factory=list)
    data_chunk: Optional[BankChunk] = None
    objects: List[HircObject] = field(default_factory=list)
    names: Dict[int, str] = field(default_factory=dict)  # STID bank names
    errors: List[str] = field(default_factory=list)
    unknown_chunks: List[BankChunk] = field(default_factory=list)
    trailing_bytes: int = 0

    @property
    def decoded_version(self) -> bool:
        return MIN_DECODED_VERSION <= self.version <= MAX_DECODED_VERSION

    def type_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for obj in self.objects:
            counts[obj.type_name] = counts.get(obj.type_name, 0) + 1
        return counts


KNOWN_CHUNKS = {"BKHD", "DIDX", "DATA", "HIRC", "STID", "STMG", "ENVS", "FXPR", "INIT", "PLAT", "AKBK"}


def is_bank(data: bytes) -> bool:
    return len(data) >= 8 and data[:4] in (b"BKHD", b"AKBK")


def read_chunks(data: bytes) -> Tuple[List[BankChunk], int]:
    chunks: List[BankChunk] = []
    off = 0
    while off + 8 <= len(data):
        tag = data[off : off + 4]
        if not all(0x30 <= b <= 0x39 or 0x41 <= b <= 0x5A for b in tag):
            break
        size = struct.unpack_from("<I", data, off + 4)[0]
        if size > len(data) - off - 8:
            break
        chunks.append(BankChunk(tag.decode("ascii"), off + 8, size))
        off += 8 + size
    return chunks, off


def parse_bank(data: bytes, decode_bodies: bool = True) -> BankInfo:
    info = BankInfo()
    if not is_bank(data):
        info.errors.append("not a Wwise bank (missing BKHD)")
        return info
    chunks, consumed = read_chunks(data)
    info.chunks = chunks
    info.trailing_bytes = len(data) - consumed
    if info.trailing_bytes:
        info.errors.append(f"{info.trailing_bytes} bytes after last well-formed chunk")
    for chunk in chunks:
        if chunk.tag == "BKHD":
            _parse_bkhd(data, chunk, info)
    for chunk in chunks:
        try:
            if chunk.tag == "DIDX":
                _parse_didx(data, chunk, info)
            elif chunk.tag == "DATA":
                info.data_chunk = chunk
            elif chunk.tag == "HIRC":
                _parse_hirc(data, chunk, info, decode_bodies)
            elif chunk.tag == "STID":
                _parse_stid(data, chunk, info)
            elif chunk.tag not in KNOWN_CHUNKS:
                info.unknown_chunks.append(chunk)
        except ParseError as exc:
            info.errors.append(f"{chunk.tag}: {exc}")
    if info.data_chunk is not None:
        base = info.data_chunk.offset
        info.media = [EmbeddedMedia(m.index, m.source_id, base + m.offset, m.size) for m in info.media]
    return info


def _parse_bkhd(data: bytes, chunk: BankChunk, info: BankInfo) -> None:
    r = BinaryReader(data, chunk.offset, chunk.offset + chunk.size)
    info.version = r.u32()
    if r.remaining() >= 4:
        info.bank_id = r.u32()
    if r.remaining() >= 4:
        info.language_id = r.u32()
    if r.remaining() >= 4:
        alt = r.u32()
        info.header_fields["alt_values"] = alt
    if info.version > 76 and r.remaining() >= 4:
        info.project_id = r.u32()
    if info.version > 141 and r.remaining() >= 4:
        info.bank_type = r.u32()
        if r.remaining() >= 16:
            info.header_fields["bank_hash"] = r.bytes(16).hex()


def _parse_didx(data: bytes, chunk: BankChunk, info: BankInfo) -> None:
    count = chunk.size // 12
    for i in range(count):
        sid, off, size = struct.unpack_from("<III", data, chunk.offset + i * 12)
        info.media.append(EmbeddedMedia(i + 1, sid, off, size))


def _parse_stid(data: bytes, chunk: BankChunk, info: BankInfo) -> None:
    r = BinaryReader(data, chunk.offset, chunk.offset + chunk.size)
    r.u32()  # string type
    count = r.u32()
    for _ in range(count):
        bank_id = r.u32()
        length = r.u8()
        name = r.bytes(length).decode("utf-8", errors="replace")
        info.names[bank_id] = name


def _parse_hirc(data: bytes, chunk: BankChunk, info: BankInfo, decode_bodies: bool) -> None:
    r = BinaryReader(data, chunk.offset, chunk.offset + chunk.size)
    count = r.u32()
    types = HIRC_TYPES_128
    for i in range(count):
        if r.remaining() < 5:
            info.errors.append(f"HIRC truncated after {i} of {count} objects")
            break
        type_code = r.u8()
        size = r.u32()
        body = r.pos
        if size > r.remaining() or size < 4:
            info.errors.append(f"HIRC object {i} has invalid size {size}")
            break
        object_id = struct.unpack_from("<I", data, body)[0]
        obj = HircObject(type_code, object_id, body, size, types.get(type_code, f"Unknown_0x{type_code:02X}"))
        obj.consumed = 4
        if decode_bodies and info.decoded_version:
            decoder = _DECODERS.get(type_code)
            if decoder is not None:
                br = BinaryReader(data, body + 4, body + size)
                try:
                    decoder(br, info.version, obj)
                    obj.consumed = br.pos - body
                    obj.parse_status = "parsed" if br.remaining() == 0 else "partial"
                    if br.remaining() and obj.fields.get("shallow_decode"):
                        obj.parse_status = "shallow"
                    elif br.remaining():
                        obj.error = f"{br.remaining()} unparsed bytes at end of object"
                except ParseError as exc:
                    obj.consumed = br.pos - body
                    obj.parse_status = "partial" if (obj.refs or obj.fields) else "failed"
                    obj.error = str(exc)
        elif decode_bodies and not info.decoded_version:
            obj.error = f"bank version {info.version} outside decoded range {MIN_DECODED_VERSION}-{MAX_DECODED_VERSION}"
        info.objects.append(obj)
        r.pos = body + size


# -------------------------------------------------------------- body decoders


def _read_props(r: BinaryReader) -> Dict[int, int]:
    count = r.u8()
    ids = [r.u8() for _ in range(count)]
    return {pid: r.u32() for pid in ids}


def _read_ranged_props(r: BinaryReader) -> Dict[int, Tuple[int, int]]:
    count = r.u8()
    ids = [r.u8() for _ in range(count)]
    return {pid: (r.u32(), r.u32()) for pid in ids}


def _read_source(r: BinaryReader, v: int, obj: HircObject, prefix: str = "source") -> Dict[str, object]:
    plugin = r.u32()
    stream_type = r.u8()
    source_id = r.u32()
    src: Dict[str, object] = {
        "plugin_id": plugin,
        "plugin_type": plugin & 0x0F,
        "codec_plugin": f"0x{plugin:08X}",
        "stream_type": SOURCE_STREAM_TYPES.get(stream_type, str(stream_type)),
        "source_id": source_id,
    }
    if v > 150:
        src["cache_id"] = r.u32()
    src["in_memory_size"] = r.u32()
    bits = r.u8()
    src["language_specific"] = bool(bits & 0x01)
    src["prefetch"] = bool(bits & 0x02)
    src["non_cachable"] = bool(bits & 0x08)
    src["has_source"] = bool(bits & 0x80)
    if (plugin & 0x0F) == 2:  # source plugin carries its parameter block
        size = r.u32()
        r.skip(size)
    obj.refs.append((prefix, source_id))
    return src


def _node_base(r: BinaryReader, v: int, obj: HircObject) -> None:
    # effects
    r.u8()  # override parent fx
    count = r.u8()
    if count:
        r.u8()
        for _ in range(count):
            r.u8()
            fx = r.u32()
            obj.refs.append(("fx", fx))
            if v <= 145:
                r.u8(); r.u8()
            else:
                r.u8()
    if v > 136:  # metadata effects
        r.u8()
        count = r.u8()
        for _ in range(count):
            r.u8(); obj.refs.append(("metadata_fx", r.u32())); r.u8()
    if 89 < v <= 145:
        r.u8()  # bOverrideAttachmentParams
    bus = r.u32()
    parent = r.u32()
    obj.fields["override_bus_id"] = bus
    obj.fields["parent_id"] = parent
    if bus:
        obj.refs.append(("output_bus", bus))
    if parent:
        obj.refs.append(("parent", parent))
    r.u8()  # priority / midi flags
    props = _read_props(r)
    if props:
        obj.fields["props"] = {f"0x{k:02X}": v2 for k, v2 in props.items()}
        loop = props.get(loop_prop_id(v))
        if loop is not None:
            obj.fields["loop_count"] = loop  # 0 = infinite in Wwise
    ranged = _read_ranged_props(r)
    if ranged:
        obj.fields["ranged_props"] = {f"0x{k:02X}": list(val) for k, val in ranged.items()}
    _positioning(r, v)
    _aux(r, v, obj)
    r.skip(6)  # advanced settings (bit vector, virtual queue, max instances, below threshold, bit vector)
    _state_chunk(r, v, obj)
    _rtpc(r, v, obj)


def _positioning(r: BinaryReader, v: int) -> None:
    bits = r.u8()
    has_positioning = bits & 1
    has_3d = (bits >> 1) & 1
    if not (has_positioning and has_3d):
        return
    r.u8()  # uBits3d
    if v <= 129:
        r.u32()
    position_type = (bits >> 5) & 3
    if position_type != 0:
        r.u8()  # path mode
        r.s32()  # transition time
        vertices = r.u32()
        r.skip(vertices * 16)
        items = r.u32()
        r.skip(items * 8)
        r.skip(items * 12)


def _aux(r: BinaryReader, v: int, obj: HircObject) -> None:
    bits = r.u8()
    if (bits >> 3) & 1:
        for _ in range(4):
            aux = r.u32()
            if aux:
                obj.refs.append(("aux_bus", aux))
    if v > 135:
        reflections = r.u32()
        if reflections:
            obj.refs.append(("reflections_aux_bus", reflections))


def _state_chunk(r: BinaryReader, v: int, obj: HircObject) -> None:
    for _ in range(r.var()):
        r.var(); r.u8()
        if v > 126:
            r.u8()
    groups = []
    for _ in range(r.var()):
        group = r.u32()
        if v > 154:
            r.u32()
        r.u8()
        states = []
        for _ in range(r.var()):
            state = r.u32()
            states.append(state)
            if v <= 145:
                r.u32()
            else:
                count = r.u16()
                r.skip(count * 2 + count * 4)
        groups.append({"state_group_id": group, "states": states})
        obj.refs.append(("state_group", group))
    if groups:
        obj.fields["state_groups"] = groups


def _rtpc(r: BinaryReader, v: int, obj: HircObject) -> None:
    count = r.u16()
    rtpcs = []
    for _ in range(count):
        rtpc_id = r.u32()
        r.u8(); r.u8()
        param = r.var() if v > 113 else r.u8()
        r.u32()  # curve id
        r.u8()  # scaling
        points = r.u16()
        r.skip(points * 12)
        rtpcs.append({"rtpc_id": rtpc_id, "param": param})
        obj.refs.append(("rtpc", rtpc_id))
    if rtpcs:
        obj.fields["rtpcs"] = rtpcs


def _children(r: BinaryReader, obj: HircObject) -> List[int]:
    count = r.u32()
    if count > 100000:
        raise ParseError(f"implausible child count {count}")
    children = [r.u32() for _ in range(count)]
    for child in children:
        obj.refs.append(("child", child))
    obj.fields["children"] = children
    return children


def _decode_sound(r: BinaryReader, v: int, obj: HircObject) -> None:
    obj.fields["source"] = _read_source(r, v, obj)
    _node_base(r, v, obj)


def _decode_action(r: BinaryReader, v: int, obj: HircObject) -> None:
    action_type = r.u16()
    target = r.u32()
    is_bus = r.u8() & 1
    obj.fields["action_type"] = action_type
    obj.fields["action_name"] = ACTION_TYPES.get(action_type, f"0x{action_type:04X}")
    obj.fields["target_id"] = target
    obj.fields["target_is_bus"] = bool(is_bus)
    if target:
        obj.refs.append(("action_target", target))
    # remaining parameters are action-specific; props bundles first
    props = _read_props(r)
    if props:
        obj.fields["props"] = {f"0x{k:02X}": val for k, val in props.items()}
    _read_ranged_props(r)
    kind = action_type & 0xFF00
    if kind in (0x0400,):  # Play: fade curve + bank id (+ bank type)
        r.u8()
        bank_id = r.u32()
        obj.fields["bank_id"] = bank_id
        if bank_id:
            obj.refs.append(("action_bank", bank_id))
        if v >= 144:
            r.u32()
    elif kind == 0x1200:  # SetState
        group, state = r.u32(), r.u32()
        obj.fields["state_group_id"], obj.fields["state_id"] = group, state
        obj.refs.append(("set_state_group", group))
        obj.refs.append(("set_state", state))
    elif kind == 0x1900:  # SetSwitch
        group, state = r.u32(), r.u32()
        obj.fields["switch_group_id"], obj.fields["switch_id"] = group, state
        obj.refs.append(("set_switch_group", group))
        obj.refs.append(("set_switch", state))
    else:
        # other action kinds: parameters after the target are not needed for
        # relationship analysis and are intentionally left undecoded
        obj.fields["shallow_decode"] = True


def _decode_event(r: BinaryReader, v: int, obj: HircObject) -> None:
    if v > 154:
        r.u8(); r.u16(); r.f32()
    count = r.var() if v > 122 else r.u32()
    actions = [r.u32() for _ in range(count)]
    obj.fields["actions"] = actions
    for action in actions:
        obj.refs.append(("event_action", action))


def _decode_ranseq(r: BinaryReader, v: int, obj: HircObject) -> None:
    _node_base(r, v, obj)
    obj.fields["loop_count"] = r.u16()
    r.u16(); r.u16()
    obj.fields["transition_time"] = r.f32()
    r.f32(); r.f32()
    r.u16()  # avoid repeat
    r.u8()  # transition mode
    r.u8()  # random mode
    mode = r.u8()
    obj.fields["mode"] = RANSEQ_MODES.get(mode, str(mode))
    bits = r.u8()
    obj.fields["continuous"] = bool(bits & 0x08)
    _children(r, obj)
    count = r.u16()
    playlist = []
    for _ in range(count):
        item, weight = r.u32(), r.s32()
        playlist.append([item, weight])
        obj.refs.append(("playlist_item", item))
    obj.fields["playlist"] = playlist


def _decode_switch(r: BinaryReader, v: int, obj: HircObject) -> None:
    _node_base(r, v, obj)
    group_type = r.u8()
    group = r.u32()
    default = r.u32()
    r.u8()
    obj.fields["group_type"] = "state" if group_type == 1 else "switch"
    obj.fields["group_id"] = group
    obj.fields["default_switch"] = default
    obj.refs.append(("switch_group", group))
    _children(r, obj)
    packages = []
    for _ in range(r.u32()):
        switch_id = r.u32()
        nodes = [r.u32() for _ in range(r.u32())]
        packages.append({"switch_id": switch_id, "nodes": nodes})
        for node in nodes:
            obj.refs.append(("switch_assoc", node))
    obj.fields["switch_packages"] = packages
    for _ in range(r.u32()):
        r.u32()
        r.u8()
        if v <= 150:
            r.u8()
        r.s32(); r.s32()


def _decode_actor_mixer(r: BinaryReader, v: int, obj: HircObject) -> None:
    _node_base(r, v, obj)
    _children(r, obj)


def _decode_layer(r: BinaryReader, v: int, obj: HircObject) -> None:
    _node_base(r, v, obj)
    _children(r, obj)
    for _ in range(r.u32()):
        r.u32()
        _rtpc(r, v, obj)
        rtpc = r.u32()
        obj.refs.append(("rtpc", rtpc))
        r.u8()
        for _ in range(r.u32()):
            child = r.u32()
            obj.refs.append(("layer_assoc", child))
            points = r.u32()
            r.skip(points * 12)
    r.u8()


def _music_node(r: BinaryReader, v: int, obj: HircObject) -> None:
    r.u8()  # flags
    _node_base(r, v, obj)
    _children(r, obj)
    grid_period = r.f64()
    grid_offset = r.f64()
    tempo = r.f32()
    beats = r.u8()
    beat_value = r.u8()
    override_meter = r.u8()
    obj.fields["meter"] = {
        "grid_period_ms": grid_period,
        "grid_offset_ms": grid_offset,
        "tempo_bpm": tempo,
        "time_signature": f"{beats}/{beat_value}",
        "override_parent": bool(override_meter),
    }
    stingers = []
    for _ in range(r.u32()):
        trigger, segment = r.u32(), r.u32()
        r.u32(); r.u32(); r.s32(); r.u32()
        stingers.append({"trigger_id": trigger, "segment_id": segment})
        if segment:
            obj.refs.append(("stinger_segment", segment))
        obj.refs.append(("stinger_trigger", trigger))
    if stingers:
        obj.fields["stingers"] = stingers


def _decode_music_segment(r: BinaryReader, v: int, obj: HircObject) -> None:
    _music_node(r, v, obj)
    obj.fields["duration_ms"] = r.f64()
    markers = []
    for _ in range(r.u32()):
        marker_id = r.u32()
        position = r.f64()
        if v <= 136:
            length = r.u32()
            name = r.bytes(length).decode("utf-8", errors="replace") if length else ""
        else:
            name = r.strz()
        markers.append({"id": marker_id, "position_ms": position, "name": name})
    obj.fields["markers"] = markers


def _decode_music_track(r: BinaryReader, v: int, obj: HircObject) -> None:
    if 89 < v <= 152:
        r.u8()
    sources = [_read_source(r, v, obj, "track_source") for _ in range(r.u32())]
    obj.fields["sources"] = sources
    if v > 152:
        r.u8()
    playlist = []
    count = r.u32()
    if count:
        for _ in range(count):
            item = {"track_index": r.u32(), "source_id": r.u32()}
            if v > 150:
                item["cache_id"] = r.u32()
            if v > 132:
                item["event_id"] = r.u32()
                if item["event_id"]:
                    obj.refs.append(("clip_event", item["event_id"]))
            item["play_at_ms"] = r.f64()
            item["begin_trim_ms"] = r.f64()
            item["end_trim_ms"] = r.f64()
            item["source_duration_ms"] = r.f64()
            playlist.append(item)
        obj.fields["subtracks"] = r.u32()
    obj.fields["playlist"] = playlist
    for _ in range(r.u32()):
        r.u32(); r.u32()
        r.skip(r.u32() * 12)
    _node_base(r, v, obj)
    track_type = r.u8()
    obj.fields["track_type"] = MUSIC_TRACK_TYPES.get(track_type, str(track_type))
    if track_type == 3:
        r.u8()
        group = r.u32()
        obj.fields["switch_group_id"] = group
        obj.refs.append(("switch_group", group))
        r.u32()
        assocs = [r.u32() for _ in range(r.u32())]
        obj.fields["switch_assocs"] = assocs
        r.skip(12 + 8 + 12)
    obj.fields["look_ahead_ms"] = r.s32()


def _music_trans_node(r: BinaryReader, v: int, obj: HircObject) -> None:
    _music_node(r, v, obj)
    rules = []
    for _ in range(r.u32()):
        src = [r.u32() for _ in range(r.u32())]
        dst = [r.u32() for _ in range(r.u32())]
        r.s32(); r.u32(); r.s32(); sync = r.u32(); r.u32(); r.u8()
        r.s32(); r.u32(); r.s32(); r.u32()
        jump_to = r.u32()
        if v > 132:
            r.u16()
        r.u16(); r.u8(); r.u8()
        rule = {"src": src, "dst": dst, "sync_type": sync, "jump_to": jump_to}
        if r.u8():
            segment = r.u32()
            r.skip(24)
            r.u8(); r.u8()
            rule["transition_segment"] = segment
            if segment and segment != 0xFFFFFFFF:
                obj.refs.append(("transition_segment", segment))
        rules.append(rule)
    obj.fields["transition_rules"] = len(rules)
    obj.fields["transition_rules_detail"] = rules[:64]


def _decode_music_switch(r: BinaryReader, v: int, obj: HircObject) -> None:
    _music_trans_node(r, v, obj)
    r.u8()  # continue playback
    depth = r.u32()
    groups = [r.u32() for _ in range(depth)]
    group_types = [r.u8() for _ in range(depth)]
    obj.fields["arguments"] = [
        {"group_id": g, "group_type": "state" if t == 1 else "switch"} for g, t in zip(groups, group_types)
    ]
    for g in groups:
        obj.refs.append(("switch_group", g))
    tree_size = r.u32()
    obj.fields["tree_mode"] = r.u8()
    end = r.pos + tree_size
    if end > r.end:
        raise ParseError("decision tree exceeds object")
    leaves: List[Dict[str, object]] = []
    _decision_tree(r, end, tree_size // 12, depth, leaves)
    r.pos = end
    obj.fields["decision_tree_leaves"] = leaves[:512]
    for leaf in leaves:
        node = int(leaf["audio_node_id"])  # type: ignore[arg-type]
        if node:
            obj.refs.append(("switch_assoc", node))


def _decision_tree(r: BinaryReader, end: int, count_max: int, max_depth: int, leaves: List[Dict[str, object]]) -> None:
    """Walk the flattened decision tree breadth-first (12-byte nodes)."""

    def walk(count: int, depth: int, path: List[int]) -> None:
        nodes = []
        for _ in range(count):
            key = r.u32()
            peek = struct.unpack_from("<I", r.data, r.pos)[0] if r.remaining() >= 4 else 0
            idx, cnt = peek & 0xFFFF, (peek >> 16) & 0xFFFF
            is_leaf = depth == max_depth or idx > count_max or cnt > count_max or r.pos + cnt * 12 > end
            if is_leaf:
                node_id = r.u32()
                r.u16(); r.u16()
                leaves.append({"path": path + [key], "audio_node_id": node_id})
                nodes.append((key, 0))
            else:
                r.u16(); child_count = r.u16()
                r.u16(); r.u16()
                nodes.append((key, child_count))
        for key, child_count in nodes:
            if child_count:
                walk(child_count, depth + 1, path + [key])

    walk(1, 0, [])


def _decode_music_ranseq(r: BinaryReader, v: int, obj: HircObject) -> None:
    _music_trans_node(r, v, obj)
    items: List[Dict[str, object]] = []
    r.u32()  # number of playlist items (total)

    def node(count: int, depth: int) -> None:
        for _ in range(count):
            segment = r.u32()
            item_id = r.u32()
            children = r.u32()
            rs_type = r.u32()
            loop = r.s16()
            r.s16(); r.s16()
            weight = r.u32()
            r.u16(); r.u8(); r.u8()
            items.append(
                {
                    "segment_id": segment,
                    "item_id": item_id,
                    "children": children,
                    "type": PLAYLIST_RS_TYPES.get(rs_type, str(rs_type)),
                    "loop": loop,
                    "weight": weight,
                    "depth": depth,
                }
            )
            if segment:
                obj.refs.append(("playlist_segment", segment))
            node(children, depth + 1)

    node(1, 0)
    obj.fields["playlist"] = items[:1024]


def _decode_state(r: BinaryReader, v: int, obj: HircObject) -> None:
    count = r.u16()
    r.skip(count * 2 + count * 4)


def _decode_bus(r: BinaryReader, v: int, obj: HircObject) -> None:
    parent = r.u32()
    obj.fields["parent_bus_id"] = parent
    if parent:
        obj.refs.append(("parent_bus", parent))
    # bus bodies differ substantially between versions; the rest is left undecoded
    obj.fields["shallow_decode"] = True


_DECODERS: Dict[int, Callable[[BinaryReader, int, HircObject], None]] = {
    0x01: _decode_state,
    0x02: _decode_sound,
    0x03: _decode_action,
    0x04: _decode_event,
    0x05: _decode_ranseq,
    0x06: _decode_switch,
    0x07: _decode_actor_mixer,
    0x08: _decode_bus,
    0x09: _decode_layer,
    0x0A: _decode_music_segment,
    0x0B: _decode_music_track,
    0x0C: _decode_music_switch,
    0x0D: _decode_music_ranseq,
    0x12: _decode_bus,
}


def heuristic_id_scan(data: bytes, obj: HircObject, known_ids: set, skip_parsed: bool = True) -> List[Tuple[int, int]]:
    """Find 32-bit values inside an object's undecoded bytes that equal known IDs.

    Returns ``[(relative_offset, id), ...]``. This is evidence, not fact: the
    caller must store these as heuristic references.
    """

    start = obj.offset + (obj.consumed if skip_parsed else 4)
    stop = obj.offset + obj.size
    hits: List[Tuple[int, int]] = []
    for pos in range(start, stop - 3):
        value = struct.unpack_from("<I", data, pos)[0]
        if value in known_ids and value not in (0, 0xFFFFFFFF, obj.object_id):
            hits.append((pos - obj.offset, value))
    return hits

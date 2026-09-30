import struct

from cstudio.formats import bnk
from cstudio.formats.soundbanksinfo import parse_soundbanksinfo
from cstudio.formats.wem import parse_wem
from cstudio.testing.builders import BankBuilder, make_soundbanksinfo, make_wem


def build_music_bank(version=150):
    b = BankBuilder("bgm", version=version)
    b.music_track(10, [111, 112], parent=20, durations_ms=[60000.0, 30000.0])
    b.music_segment(20, [10], parent=30, duration_ms=60000.0, markers=[(0, 0.0, "Entry"), (1, 59000.0, "Exit")])
    b.music_ranseq(30, [20], parent=40)
    b.music_switch(40, 999, {1: 30, 2: 30})
    b.event("Play_Music", [b.action_play(50, 40)])
    b.sound(60, 222, parent=70, loop=0)
    b.ranseq(70, [60])
    b.embed(222, make_wem(seconds=2.0))
    return b


def test_bank_parse_all_objects_decoded():
    for version in (135, 145, 150, 154):
        data = build_music_bank(version).build()
        info = bnk.parse_bank(data)
        assert info.version == version
        assert info.errors == []
        statuses = {o.type_name: o.parse_status for o in info.objects}
        assert all(s == "parsed" for s in statuses.values()), (version, statuses)
        assert info.names[info.bank_id] == "bgm"
        assert [m.source_id for m in info.media] == [222]


def test_music_fields():
    info = bnk.parse_bank(build_music_bank().build())
    objs = {o.object_id: o for o in info.objects}
    track = objs[10]
    assert [s["source_id"] for s in track.fields["sources"]] == [111, 112]
    assert track.fields["playlist"][1]["source_duration_ms"] == 30000.0
    seg = objs[20]
    assert seg.fields["duration_ms"] == 60000.0
    assert [m["name"] for m in seg.fields["markers"]] == ["Entry", "Exit"]
    assert seg.fields["meter"]["tempo_bpm"] == 120.0
    switch = objs[40]
    assert ("switch_group", 999) in switch.refs
    assert {leaf["audio_node_id"] for leaf in switch.fields["decision_tree_leaves"]} == {30}
    assert objs[60].fields["loop_count"] == 0  # infinite loop property
    event = [o for o in info.objects if o.type_name == "Event"][0]
    assert event.fields["actions"] == [50]
    assert objs[50].fields["target_id"] == 40


def test_unknown_type_and_truncation_are_tolerated():
    b = build_music_bank()
    b.raw_object(0x33, 777, b"\x00" * 8)
    data = b.build()
    info = bnk.parse_bank(data)
    unknown = [o for o in info.objects if o.object_id == 777][0]
    assert unknown.parse_status == "header_only" and unknown.type_name.startswith("Unknown")
    truncated = bnk.parse_bank(data[: len(data) // 2])
    assert truncated.version == 150  # still identifies the bank
    assert bnk.parse_bank(b"not a bank").errors


def test_corrupt_object_body_is_flagged_not_fatal():
    b = BankBuilder("x")
    b.raw_object(0x0B, 5, b"\xff" * 6)  # a MusicTrack with a nonsense body
    info = bnk.parse_bank(b.build())
    obj = info.objects[0]
    assert obj.parse_status in ("failed", "partial")
    assert obj.error


def test_unsupported_version_keeps_headers():
    info = bnk.parse_bank(build_music_bank(version=150).build().replace(struct.pack("<I", 150), struct.pack("<I", 88), 1))
    assert info.version == 88
    assert not info.decoded_version
    assert all(o.parse_status == "header_only" for o in info.objects)


def test_heuristic_scan_finds_known_ids():
    b = BankBuilder("x")
    b.raw_object(0x2A, 1, struct.pack("<II", 4242, 7))
    info = bnk.parse_bank(b.build())
    data = b.build()
    hits = bnk.heuristic_id_scan(data, info.objects[0], {4242})
    assert hits and hits[0][1] == 4242


def test_wem_vorbis_and_pcm():
    v = parse_wem(make_wem(seconds=90.0, channels=2, sample_rate=48000, loop=(0, 100)))
    assert v.valid and v.codec == "Wwise Vorbis" and v.channels == 2
    assert v.duration_seconds == 90.0 and v.duration_method == "fmt_sample_count"
    assert v.loops[0]["end_sample"] == 100
    p = parse_wem(make_wem(format_tag=0x0001, channels=1, sample_rate=22050, seconds=2.0))
    assert p.codec == "PCM" and p.duration_method == "exact_pcm" and abs(p.duration_seconds - 2.0) < 0.01


def test_wem_prefix_and_garbage():
    full = make_wem(seconds=30.0)
    head = parse_wem(full[:200], total_size=len(full))
    assert head.valid and head.duration_seconds == 30.0
    assert not parse_wem(b"RIFF\x00\x00").valid
    assert not parse_wem(b"\x00" * 100).valid


def test_soundbanksinfo():
    xml = make_soundbanksinfo({"bgm": {"events": ["Play_A"], "memory": {5: "a.wav"}, "streamed_refs": [6]}}, {6: "b.wav"})
    info = parse_soundbanksinfo(xml)
    assert info.recognized_schema and info.soundbank_version == "150"
    assert [b.name for b in info.banks] == ["bgm"]
    assert info.events[0].name == "Play_A" and info.events[0].bank_id == info.banks[0].bank_id
    rel = {(m.media_id, m.relation) for m in info.media}
    assert (5, "included_in_memory") in rel and (6, "streamed") in rel and (6, "referenced_streamed") in rel
    names = info.names_by_id()
    assert "BGM_Region" in names.values() and "Desert" in names.values()


def test_soundbanksinfo_new_schema_and_unknown_xml():
    xml = b"""<?xml version="1.0"?><SoundBanksInfo SchemaVersion="16"><Media>
      <File Id="42" Language="SFX" Streaming="true" Location="Loose"><ShortName>x.wav</ShortName><CachePath>x_1a2b.wem</CachePath></File>
    </Media><SoundBanks><SoundBank Id="7" Language="SFX"><ShortName>b</ShortName><Media><File Id="42"/></Media></SoundBank></SoundBanks></SoundBanksInfo>"""
    info = parse_soundbanksinfo(xml)
    media = {(m.media_id, m.bank_id): m for m in info.media}
    assert media[(42, None)].streaming is True and media[(42, None)].cache_path == "x_1a2b.wem"
    assert (42, 7) in media
    odd = parse_soundbanksinfo(b"<Sound><Bank id='3' name='z'/></Sound>")
    assert not odd.recognized_schema and odd.named_objects[0].name == "z"
    broken = parse_soundbanksinfo(b"<a><b></a>")
    assert broken.errors
    fragments = parse_soundbanksinfo(b"<Bank Id='1' Name='one'/><Bank Id='2' Name='two'/>")
    assert len(fragments.named_objects) == 2

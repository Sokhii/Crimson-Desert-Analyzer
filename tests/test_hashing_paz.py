import struct

import lz4.block
import pytest

from cstudio.formats.hashing import hashlittle, wwise_fnv1_32
from cstudio.formats.paz import (ArchiveError, ArchiveReader, build_pamt_v2, chacha20_xor, derive_chacha20_key_nonce,
                                 parse_pamt_bytes, parse_papgt)


def test_lookup3_known_value():
    # published example from the community key-derivation notes
    assert hashlittle(b"rendererconfigurationmaterial.xml", 0xC5EDE) == 0xAF3DCEF3


def test_key_derivation_example():
    key, nonce = derive_chacha20_key_nonce("some/dir/RendererConfigurationMaterial.xml")
    assert nonce == bytes.fromhex("f3ce3daf") * 4
    assert key.hex() == "90ac5ccf9aa656c59ca050c396aa5ac99ea252c19aa656c596aa5ac992ae5ecd"


def test_wwise_fnv():
    assert wwise_fnv1_32("init") == 1355168291
    assert wwise_fnv1_32("Init") == 1355168291  # case-insensitive


def test_lookup3_lengths_do_not_crash():
    for n in range(0, 40):
        hashlittle(bytes(range(n)))


def test_pamt_roundtrip_and_read(tmp_path):
    content = b"<?xml version='1.0'?><SoundBanksInfo/>" * 10
    stored = lz4.block.compress(content, store_size=False)
    enc = chacha20_xor(stored, "soundbanksinfo.xml")
    plain = b"BKHD" + b"\x00" * 20
    paz = enc + plain
    records = [("sound/soundbanksinfo.xml", 0, 0, len(enc), len(content), 2 | (3 << 4)),
               ("sound/x.bnk", 0, len(enc), len(plain), len(plain), 0)]
    pkg = tmp_path / "0004"
    pkg.mkdir()
    (pkg / "0.paz").write_bytes(paz)
    (pkg / "0.pamt").write_bytes(build_pamt_v2(records, [len(paz)]))
    index = parse_pamt_bytes((pkg / "0.pamt").read_bytes(), pkg / "0.pamt")
    assert index.layout == "v2"
    paths = {e.path: e for e in index.entries}
    assert set(paths) == {"sound/soundbanksinfo.xml", "sound/x.bnk"}
    xml = paths["sound/soundbanksinfo.xml"]
    assert xml.encrypted and xml.compression_type == 2
    with ArchiveReader() as reader:
        assert reader.read(xml) == content
        assert reader.read(paths["sound/x.bnk"]) == plain
        assert reader.read(paths["sound/x.bnk"], limit=4) == b"BKHD"


def test_pamt_garbage_rejected(tmp_path):
    with pytest.raises(ArchiveError):
        parse_pamt_bytes(b"\x01\x02\x03", tmp_path / "0.pamt")
    with pytest.raises(ArchiveError):
        parse_pamt_bytes(b"\xff" * 64, tmp_path / "0.pamt")


def test_papgt():
    names = b"0000\x000004\x00"
    body = struct.pack("<III", 0x7FFF00, 0, 1) + struct.pack("<III", 0x7FFF00, 5, 2) + struct.pack("<I", len(names)) + names
    data = struct.pack("<III", 0, 0, 2) + body
    assert [n for n, _f, _c in parse_papgt(data)] == ["0000", "0004"]

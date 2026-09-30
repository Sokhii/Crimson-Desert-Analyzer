# Crimson Desert archive format (PAZ / PAMT / PAPGT)

Trust labels used in this folder:

| Label | Meaning |
|---|---|
| **Community-confirmed** | Documented by one or more public community tools that read real game data. |
| **Experimentally verified** | Reproduced by this project against real data or published test vectors. |
| **Inferred** | Our interpretation, consistent with the evidence but not proven. |
| **Unknown** | Open question. |

## Sources

- `lazorr410/crimson-desert-unpacker` (MIT): C++/C#/Python unpacker; `PAZ_DECRYPTION.md` documents key derivation. <https://github.com/lazorr410/crimson-desert-unpacker>
- `Ratty123/crimson-desert-mod-workbench` (CDMW, MIT): Python/PySide6 workbench; `cdmw/core/archive_format.py`, `papgt_format.py`, `archive_wwise_bank.py`. <https://github.com/Ratty123/crimson-desert-mod-workbench>
- `hzeemr/crimsonforge` (MIT): modding studio; `core/audio_index.py` documents audio package groups. <https://github.com/hzeemr/crimsonforge>
- `Ekey/CD.PAZ.Tool`: extractor (no format notes in its README).

No code was copied from these projects. Our implementation (`src/cstudio/formats/paz.py`) was written from the format descriptions below.

## Layout

- Package directories `0000` … `0035` each hold `0.pamt` (index) and `N.paz` (data). **Community-confirmed** (all sources).
- `meta/0.papgt` lists mounted package directories in priority order; the first directory holding a path wins, which is how mods overlay files. **Community-confirmed** (CDMW `papgt_format.py`).
- Package `0004` holds SFX/music (numbered IDs, no voice); `0005`/`0006`/`0035` hold voice languages. **Community-confirmed** (CrimsonForge `audio_index.py`, v1.2.0 of the game; may change between game versions).
- The Wwise metadata file lives at `sound/soundbanksinfo.xml` inside the archives. **Community-confirmed** (CrimsonForge `character_asset_resolver.py`). A community-cleaned `soundbank.xml` exists as a Nexus mod ("CD Soundbank Clean Edition" by MichiModding), which confirms the original XML is hard to read but present.

## PAMT (current layout, "v2")

**Community-confirmed** (CDMW); implemented in `_parse_layout_v2`:

```
u32 header_crc, u32 paz_count, u32 unknown
paz_count × { u32 hash, u32 size, u32 unknown }         (12 bytes each)
u32 dir_block_size,  dir_block   : name fragments {u32 parent_offset, u8 len, bytes name}
u32 name_block_size, name_block  : same fragment encoding for file names
u32 folder_count, folder_count × { u32 hash, u32 dir_name_offset, u32 first_file_index, u32 file_count }
u32 file_count,   file_count × { u32 name_offset, u32 paz_offset, u32 comp_size, u32 orig_size, u16 paz_index, u16 flags }
```

Paths are built by walking parent offsets (0xFFFFFFFF = root) and concatenating fragments.

`flags`: low nibble = compression (0 none, 1 partial [DDS only], 2 LZ4 block, 3/4 zlib/QuickLZ per CDMW), next nibble = encryption (0 none, 1 ICE, 2 AES, 3 ChaCha20). **Community-confirmed**; the meaning of compression types 3/4 differs between sources (**Unknown** which is right - the analyzer tries zlib and reports failures as unknown structures).

An earlier layout ("v1", documented by crimson-desert-unpacker) is used as a fallback parser.

## Encryption

ChaCha20 with a 32-byte key and 16-byte nonce derived from the **lowercase basename** only:

```
seed  = lookup3_hashlittle(basename.lower(), initval=0x000C5EDE)
nonce = pack('<I', seed) * 4
key   = concat(pack('<I', (seed ^ 0x60616263) ^ d) for d in
               [0, 0x0A0A0A0A, 0x0C0C0C0C, 0x06060606, 0x0E0E0E0E, 0x0A0A0A0A, 0x06060606, 0x02020202])
```

**Community-confirmed** and **Experimentally verified**: our implementation reproduces the published test vector (`rendererconfigurationmaterial.xml` → seed `0xAF3DCEF3`, key `90ac5ccf…92ae5ecd`) - see `tests/test_hashing_paz.py`.

Order of operations: data is compressed, then encrypted. Reading = decrypt → decompress. Because ChaCha20 is a stream cipher, a *prefix* of an uncompressed encrypted entry decrypts correctly on its own; the analyzer uses this to read only WEM headers.

## Integrity / modding constraints (for the future mod compiler)

- PAMT is integrity-checked; the checksum chain runs PAPGT → PAMT (CDMW `papgt_format.py`, `archive_patching.py`). **Community-confirmed**.
- Overlay mods add a new numbered directory with its own `0.pamt`/`0.paz` and list it first in `meta/0.papgt`. **Community-confirmed** (CDMW). This is the route a *separate* soundtrack mod should use later - never patching shipped archives.
- The analyzer is read-only and does none of this.

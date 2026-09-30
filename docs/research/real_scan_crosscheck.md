# First real-installation scan and community CSV cross-check (2026-09-30)

Source: the project owner's scan of the Steam installation (34 packages, 2,052,523 archive entries,
3,288 banks, 226,538 media IDs, 248 s) compared with the community "Ultimate Soundbank Guide" CSV
(1,481 rows: 1,009 Music, 472 Ambience; not redistributed).

## Experimentally verified

| Fact | Evidence |
|---|---|
| Crimson Desert ships Wwise bank version **150** (all 3,288 banks) | `soundbanks.json` |
| The v150 HIRC decoders fit the real data | 232,251 objects `parsed`, 837 `shallow` (by design), 22,023 `header_only` (FxCustom, Attenuation, FxShareSet, modulators, AudioDevice - types not decoded yet); **0 failed/partial** |
| Banks are stored as `sound/windows/<bank id>.bnk`, media as `sound/windows/media/<source id>.wem` | file paths in the report |
| `412724365.bnk` = FNV-1("bgm"); `2113151378.bnk` = FNV-1("bgm_playlist") | hash match; all 53 bank names in the CSV exist as bank files |
| Wwise Vorbis `fmt`+0x18 holds the sample count | for all 1,137 music media the WEM-header duration equals the MusicTrack clip `fSrcDuration` within 0.022 s (1,064 within 0.01 s) |
| Music media: Wwise Vorbis, 48 kHz, 1,133 stereo / 3 mono / 1 quad; 1,129 prefetch-streamed, 4 streamed, 4 embedded; no `smpl` loop points (looping is done by the music hierarchy) | `music_assets.json` |
| `1981912997.bnk` has the same object/media counts as `bgm` (5,831 objects, 1,104 embedded media, 2,019 MusicTracks); its name is **Unknown** | `soundbanks.json` |

## CSV vs analyzer

- 997 of 1,009 CSV "Music" rows were identified as music (98.8 %); 1,008 of 1,008 matched rows sit in exactly the bank the CSV names.
- 6 CSV music IDs do not exist anywhere in this installation (no bank reference, no WEM) - likely cut or changed in a game update.
- 6 CSV music IDs are played by plain `Sound` objects (not MusicTrack): `indoor_choir_sing`, three `stonecrab_gashole_sub_lp_*` geyser loops, two `cd_seq_11_main_quest_intro` stems.
- 11 CSV "Ambience" rows are `BGM_CD_Ambient_*` tracks inside the `bgm` / `bgm_playlist` music hierarchy - ambient exploration *music*; the analyzer labels them music.
- 461 CSV Ambience rows: none labelled music by the analyzer (all plain `Sound` objects).
- 129 analyzer music items are missing from the CSV; all are MusicTracks under MusicSegment → playlist → MusicSwitch in `bgm` (115), `925595748.bnk` (9) and `2498340951.bnk` (5), 7 s - 7.8 min.

## Analyzer issues found and fixed

- `media_context.banks` included every bank holding any *ancestor* container; shared parent mixers are duplicated into hundreds of banks, which inflated `relationships.json` to 4.7 GB and attached unrelated bank names to sounds. Banks now come from owning objects and embedding banks only; `relationships.json` is written compact.
- No bank names were resolved because the game ships no SoundbanksInfo; importing the CSV provides hash-verified bank names.

## Re-scan with the fixed build (2026-09-30, 14:27)

- `relationships.json` 4.7 GB → 102 MB; scan time 248 s → 124 s.
- Music results identical to the first scan: same 1,137 media IDs, same durations/codec/channels/rates, same file
  hashes, same container chains, same soundbanks.
- With media bank lists no longer inflated (most media now list 1-2 banks: `bgm` and its twin), the CSV's bank
  column could be checked for **every** CSV ID present in the scan: 1,475 / 1,475 match (music and ambience).

## Music switch decision trees (verified on the exported `412724365.bnk`, 2026-09-30)

- **Experimentally verified:** the decision tree is a flat array of 12-byte nodes `{key, uIdx:u16|uCount:u16 or
  audioNodeId, weight, probability}`; node 0 is the root and a branch's children are `nodes[uIdx : uIdx+uCount]`.
  Children are *not* stored in reading order for multi-argument trees (e.g. a 3-argument tree whose first level
  points to indices 181, 187, 193, ...). Sequential reading produced wrong leaves for 3-argument switches; walking
  by index fixes it (`102548524`: 717/717 leaves resolve to MusicRandomSequenceContainers).
- **Experimentally verified:** switch containers with no arguments carry a single root node and select nothing
  (previously mis-read as a leaf pointing at id `1`).
- **Experimentally verified:** `bgm` contains two "mirror" switch containers under parent `363776544`:
  `725591625` mirrors `102548524` (identical 3 state groups and 717 key paths) and `919159439` mirrors
  `244399752` (same state group, 1,469 of its state keys). Every leaf of the mirrors points to an id that exists in
  no bank of the installation (206 and 1,452 distinct 30-bit ids). The real hierarchy is under `347759289`, which
  the bank's only event plays. These dangling references are shipped data (cut/unused content), not a parser error.
- **Inferred:** music state groups (e.g. `1788622765`) are never set by any Wwise action in any bank, so the game
  code sets them directly.

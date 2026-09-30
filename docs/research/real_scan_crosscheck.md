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

# Community audio research: what exists and how we use it

## Ultimate Soundbank Guide (Nexus, CSV)

A community CSV mapping media IDs to descriptions, contexts, categories, original file names and source banks. Columns:
`Audio ID, English Description, Location / Context, Category, Original File Name, SoundBank Source`.

Observations from the copy supplied by the project owner (not redistributed here):

- 1,481 rows: 1,009 `Music`, 472 `Ambience`.
- 999 rows come from the `bgm` bank; others from `env_*`, `sfx_*`, `cd_seq_*` and `cd_playing_panflute_*` banks, plus `bgm_playlist` and `choir`.
- Original names follow Wwise source file names, e.g. `bgm_cd_region_big_desert_1normal_joo_01_v01.wav`, `CD_Field_Desert-01.wav`, `env__region__desert__2d__day__lp.wav`, and Korean titles for story cues.
- Names encode musical structure hints that matter for future matching (e.g. `bpm082`, `stem1..4`, `intro/outro`, `1normal/2tensioned`, `free tempo`).

Status: **Community-confirmed** (it is a community artefact), **not** parser-verified. The analyzer imports such files as a separate `research_source` and `community_media` table and only uses them as labelled evidence. `community.cross_validate()` checks each row against the scan (does the media exist; does the claimed bank's FNV hash match the bank that actually references the media) so a report can say which claims were *experimentally verified*.

Import: Home → "Import community CSV…" or `CrimsonSoundtrackStudio.exe --import-csv guide.csv`.

## CD Soundbank Clean Edition (Nexus, MichiModding)

A reorganised, partially translated `soundbank.xml`. Confirms that the game's own audio XML exists, is hard to read and encodes grouping of audio events. The analyzer's XML parser is schema-tolerant for that reason and reports unrecognised elements as unknown structures instead of discarding them. **Community-confirmed**; format details **Unknown** (Nexus pages were not reachable from the build environment).

## Soundtrack replacement mods

- *The Crimson Tamriel* / *Crimson Tamriel V2* - replace exploration music; V3 notes patching "internal music timeline metadata inside the original `.bnk` files" because longer replacement WEMs were cut/restarted by the old timing.
- *Way To Valhalla Music Overhaul* - replaces 600+ tracks.
- *Crimson Hunt* - replaces the main-menu music (Witcher 3 "The Trail"); installed through JSON Mod Manager / Crimson Browser & Mod Manager.
- *Echoes of Pywel* - full audio overhaul with heavy Wwise/BNK editing.

Take-aways (**Community-confirmed**): music playback length and transitions are driven by BNK interactive-music metadata (segment duration, markers, track clip ranges), not by WEM length. The analyzer therefore records `MusicSegment.duration_ms`, entry/exit markers and track clip `source_duration_ms` for every music asset, next to the WEM's own duration - the future replacement compiler will need both.

## Tools

| Tool | What it confirms for us |
|---|---|
| CDMW (Ratty123) | PAMT layout, PAPGT mount order, ChaCha20/LZ4, BNK DIDX handling, `.wem` naming by source id |
| crimson-desert-unpacker (lazorr410) | Deterministic ChaCha20 key derivation + test vector |
| CrimsonForge (hzeemr) | Package 0004 = SFX/music, voice packages per language, `sound/soundbanksinfo.xml` path |
| wwiser (bnnm) | HIRC field layouts per bank version |
| vgmstream | WEM codec identification and sample-count conventions |

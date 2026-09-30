# Open questions (seed list for the AI investigator and for humans)

Each item is **Unknown** until a real-installation scan answers it. Findings go to `data/knowledge/`; confirmed ones should become parser code plus a regression test.

1. Which Wwise bank version(s) does Crimson Desert ship? (Recorded per bank in `soundbanks.json`.) If it is outside 128–172 the HIRC body decoders must be extended.
2. Are music WEMs stored uncompressed and unencrypted in PAZ (expected: already-compressed audio, flags 0)? `get_file_metadata` shows compression/encryption per entry.
3. Is `sound/soundbanksinfo.xml` encrypted (ChaCha20) and LZ4-compressed like other XML?
4. Which schema does the game's SoundbanksInfo use (old `StreamedFiles`/`IncludedMemoryFiles` vs newer `Media` table)?
5. What drives music selection - state groups (e.g. region/tension) or switch groups? Music switch containers' `arguments` answer this once group IDs are named.
6. What do `1normal` / `2tensioned`, `stem1..4` and `p01..p04` in source names correspond to structurally (layers, sub-tracks, playlist steps)?
7. Do any Pearl Abyss-specific HIRC object types or chunks exist (they would show up as `hirc_type:*` / `bnk_chunk:*` unknowns)?
8. Where do game data tables (`gamedata/*.pabgb`) reference audio events? `extract_strings` + `hash_name` can connect event names used by gameplay to Wwise event IDs.

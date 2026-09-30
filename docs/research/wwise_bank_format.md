# Wwise soundbank / media knowledge used by the analyzer

## Sources

- `bnnm/wwiser` - Wwise `.bnk` explorer that documents every HIRC field per bank version (<https://github.com/bnnm/wwiser>). Used as a *format reference only*; no code copied (the repository did not show a licence file when checked).
- CDMW `archive_wwise_bank.py` (MIT) - DIDX/DATA handling and the "embedded media are subsongs; streamed media are `<source id>.wem`" convention.
- Wwise public documentation (Audiokinetic) for concepts: Events, Actions, containers, interactive music (segments, tracks, switch / playlist containers), SoundbanksInfo.
- Community soundtrack mods on Nexus (e.g. "The Crimson Tamriel", "Crimson Tamriel V2", "Way To Valhalla Music Overhaul", "Crimson Hunt"): their descriptions state that replacing WEMs is not enough because the game keeps cutting/restarting music using timing stored in the original `.bnk` (music timeline metadata). **Community-confirmed** that Crimson Desert uses Wwise *interactive music* objects with durations/markers in BNKs - exactly what `MusicSegment.duration_ms`, markers and `MusicTrack` clip `source_duration_ms` capture.

## Facts the parser relies on

| Fact | Status |
|---|---|
| BNK = chunk list `tag(4) size(u32) payload`; `BKHD` first, version = first u32 of BKHD | Community-confirmed (wwiser, CDMW) |
| `DIDX` = 12-byte records `{source_id, offset, size}` into `DATA` | Community-confirmed |
| `HIRC` = `u32 count` then objects `{u8 type, u32 size, u32 id, body}` | Community-confirmed |
| Type codes for bank version ≥ 128: 2 Sound, 3 Action, 4 Event, 5 RanSeq, 6 Switch, 7 ActorMixer, 9 Layer, 10 MusicSegment, 11 MusicTrack, 12 MusicSwitch, 13 MusicRanSeq … | Community-confirmed (wwiser) |
| Named-object IDs = FNV-1 32-bit of the lowercase name | Community-confirmed; Experimentally verified (`init` → 1355168291) |
| Media (WEM) IDs are *not* name hashes | Community-confirmed |
| Streamed media are stored as `<source id>.wem`; newer Wwise also uses `<name>_<hash>.wem` cache paths listed in SoundbanksInfo | Community-confirmed (CDMW, web search snippet of the game's soundbanksinfo showing `reaction_crowds_when_bear_attack_2021b791.wem`) |
| Crimson Desert bank version | **Unknown** - the analyzer records it per bank; the body decoders cover versions 128–172 and flag anything else |

## Decoded object bodies

Implemented in `src/cstudio/formats/bnk.py` following wwiser's field order for versions ≥ 128:

- Sound: `AkBankSourceData` (plugin, stream type, **source id**, [cache id v>150], in-memory size, bits, plugin params) + `NodeBaseParams`.
- NodeBaseParams: FX, metadata FX (v>136), attachment flag (v 90–145), **bus**, **parent**, props (incl. `Loop`), ranged props, positioning, aux, advanced settings, state chunk, RTPC curves.
- Action: type, **target id**, props; Play keeps bank id; SetState / SetSwitch keep group and state. Other action kinds are decoded only to the target ("shallow", by design).
- Event: action list (var-int count v>122; extra header v>154).
- Containers: children, playlists, switch packages.
- Music: meter/tempo, stingers, **segment duration and markers**, **track sources and clip playlist (play-at, trims, source duration)**, track switch params, transition rules (incl. transition segments), music switch decision tree leaves, music playlist tree.

Every object keeps `parse_status` (`parsed`/`partial`/`shallow`/`header_only`/`failed`). Non-parsed objects get a *heuristic* scan for known IDs inside their undecoded bytes; those references are stored with `confidence='heuristic'` and never mixed with parsed references.

## WEM headers

RIFF/RIFX `WAVE` with Wwise format tags: `0xFFFF` Vorbis, `0x3040`/`0x3041` Opus, `0x0002` Wwise IMA ADPCM, `0x0001`/`0xFFFE` PCM … (vgmstream conventions, community-confirmed). Duration is taken from: exact PCM math; the sample count in the Wwise `fmt` extension at `+0x18` (**Inferred** from vgmstream behaviour, cross-checked against the byte-rate estimate before being trusted); else the byte-rate estimate. `smpl` loop points, `cue ` points and `LIST/adtl` labels are read when present.

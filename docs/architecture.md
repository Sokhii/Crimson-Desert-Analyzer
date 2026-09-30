# Architecture

```
            ┌──────────────── GUI (PySide6, src/cstudio/ui) ────────────────┐
            │  Home · Scan · Results · AI Investigation  (presentation only)│
            └───────────────┬──────────────────────────────┬────────────────┘
                            │ services.Studio               │ QThread workers
     ┌──────────────────────┴───────┐          ┌───────────┴─────────────────┐
     │ Layer A: deterministic        │          │ Layer B: local AI            │
     │ analyzer (src/cstudio/analyzer)│          │ (src/cstudio/ai)             │
     │  discovery → PAMT index →      │          │  catalog / downloader        │
     │  cached BNK/WEM/XML analysis → │  facts   │  runtime (llama-server)      │
     │  names → relationships →       │ ───────► │  tools (read-only + record)  │
     │  music evidence → unknowns →   │          │  agent (iterative loop)      │
     │  reports                       │ ◄─────── │                              │
     └──────────────┬────────────────┘ findings └──────────────┬───────────────┘
                    │                                           │
         ┌──────────┴───────────┐                   ┌───────────┴───────────┐
         │ formats/ (pure       │                   │ knowledge/ store       │
         │ parsers: paz, bnk,   │                   │ findings + JSON mirror │
         │ wem, soundbanksinfo) │                   └───────────┬───────────┘
         └──────────────────────┘                               │
                         └──────── SQLite (db/, versioned) ─────┘
```

## Rules

- **Read-only game access.** Only `formats/paz.ArchiveReader`, the scanner's loose-file reader and the AI tools open game files, always with `"rb"`. Tests assert that a scan leaves the installation byte- and timestamp-identical.
- **Portable data.** `app_paths.AppPaths` derives every directory from the executable location (`sys.executable` when frozen). Process temp is redirected into `temp/`, llama.cpp's cache into `cache/`.
- **Deterministic facts vs AI claims.** Parser output lives in `asset/bnk/wem/wwise_object/object_ref/...`. AI output lives only in `finding` (+ JSON files) with status `verified | probable | hypothesis | unknown | rejected`. `verified` requires a deterministic check (`tools.run_check`). Heuristic references are stored with `confidence='heuristic'`.
- **Knowledge reuse.** Verified name mappings are merged into the `name` table on every scan; explanations of unknown-structure signatures mark matching unknowns `explained`. Promoting knowledge into parser code is a deliberate code change plus a regression test.
- **Replaceable AI.** The agent depends only on `ai.runtime.InferenceBackend` (`chat(messages, json_schema=...)`). llama.cpp is the bundled implementation; an OpenAI-compatible endpoint and a scripted test backend also exist.

## Database (schema v1, `db/schema.py`)

| Concept | Tables |
|---|---|
| GameInstallation / Scan | `installation`, `scan`, `source_file`, `archive_entry` |
| File (analyzed) | `asset` (archive entry, loose file or bank-embedded media; fingerprint for caching) |
| BNK | `bnk` (+ `metadata` for STID names, unknown chunks) |
| WEM | `wem` (codec, channels, rate, duration + method, loops, cues) |
| WwiseObject / Event / Container | `wwise_object` (type, parse status, decoded fields) |
| Reference | `object_ref` (kind, parsed/heuristic), `media_source` (object → media) |
| Metadata | `xml_doc`, `xml_bank`, `xml_event`, `xml_media`, `xml_object`, `name`, `metadata` |
| Relationships (derived) | `media_context` (owners, container chain, events, banks per media) |
| Classification | `classification` (role, score, confidence, evidence list) |
| Unknowns | `unknown_structure` (signature, occurrences, examples, status) |
| Finding / Hypothesis | `finding` |
| ResearchSource | `research_source`, `community_media` |
| Model | `model`; AI audit trail in `ai_session`, `ai_step` |

Migrations are append-only and tracked with `PRAGMA user_version`; a database from a newer app version is refused rather than damaged.

## Caching

- PAMT indexes are re-parsed only when the `.pamt` size/mtime changes.
- Each analyzed asset stores a fingerprint of its archive identity (package, path, PAZ index, offset, sizes, flags, PAZ size/mtime) or loose size/mtime, plus the parser version. Unchanged assets are skipped; bumping `PARSER_VERSION` re-analyzes everything.
- WEMs are read only up to 256 KiB (headers); BNKs and XML are read fully.

## Output (`data/scans/scan_NNNNN/`, copied to `data/reports/latest/`)

`scan.json`, `music_assets.json`, `soundbanks.json`, `relationships.json`, `findings.json`, `unknown_structures.json` (all wrapped in a versioned envelope `{schema, schema_version, generator, scan_id, installation, data}`) and `report.html` for humans.

## Future work hooks (not implemented in this milestone)

- `ai.runtime.InferenceBackend` is reused by the future thematic matching layer.
- `music_assets.json` already carries the structural facts a replacement compiler needs: media id, container chain, segment duration/markers, track clip ranges, streaming type, loop points, codec/channels/rate.
- The overlay-package approach documented in `docs/research/crimson_desert_archives.md` is the intended route for a *separate* soundtrack mod.

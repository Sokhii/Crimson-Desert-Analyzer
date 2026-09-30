# Crimson Soundtrack Studio

A portable Windows application that analyzes the Wwise audio of **Crimson Desert** and includes a **local AI reverse‑engineering agent** that investigates whatever the parser doesn't understand yet.

This is the first milestone: the **Crimson Desert Analyzer** and the **local AI investigator**. Soundtrack replacement (analyzing your FLAC library, matching music, converting audio, building the mod) comes later. The data model is already set up for it.

## What it does

- Reads your Crimson Desert installation **without changing anything**. It indexes the PAZ/PAMT archives, decrypts and decompresses entries in memory, and finds the audio-related files.
- Parses Wwise `.bnk` soundbanks: chunks, embedded media, and the HIRC object hierarchy, including the interactive‑music objects (segments, tracks, switch and playlist containers, durations, markers, tempo). It also reads `.wem` headers (codec, channels, sample rate, duration, loops) and `SoundbanksInfo` XML.
- Builds a relationship database across all banks (event → action → containers → sound or music track → WEM) and names IDs where it can. Names for banks, events and states are checked against their Wwise hash.
- Identifies likely music and records the evidence and a confidence for every decision. It doesn't sort tracks into gameplay categories.
- Keeps every unknown or unresolved structure as a work item instead of dropping it.
- Caches results, so a rescan of an unchanged game takes seconds.
- Writes JSON output (`scan.json`, `music_assets.json`, `soundbanks.json`, `relationships.json`, `findings.json`, `unknown_structures.json`) plus an HTML report.
- Runs a local LLM through the bundled **llama.cpp** runtime, with no LM Studio, Ollama or Python to install. You pick a Low, Medium or High tier or your own GGUF file. The app downloads the model from Hugging Face, checks it against its SHA‑256, loads it and runs a test prompt.
- Runs an **autonomous investigation**. The AI works through the scan's open questions using controlled read‑only tools and records hypotheses and findings. A finding only becomes *verified* when a deterministic check passes, such as a name‑hash match or a byte pattern. You can pause or stop it at any time.
- Keeps AI knowledge between runs, both in the database and as JSON under `data/knowledge/`. A structure explained once is recognised automatically in later scans.

## Quick start (portable build)

1. Download `CrimsonSoundtrackStudio-<version>-windows-portable.zip` from Releases or from the latest `windows-portable` Actions run.
2. Extract it to a folder you can write to (not Program Files).
3. Run `CrimsonSoundtrackStudio.exe`.
4. **Home** tab: browse to your Crimson Desert folder, choose an AI tier and press **Download Model** (optional), then press **Analyze Game** or **Analyze + AI Investigation**.

Everything the program creates (database, scans, knowledge, reports, models, logs, config) is stored inside its own folder. If you move the folder, the data moves with it.

| Tier | Default model | Download | Approx. VRAM |
|---|---|---|---|
| Low | Qwen3 4B Instruct 2507 Q4_K_M | ~2.5 GB | 4–6 GB |
| Medium | Qwen3 8B Q4_K_M | ~5 GB | 8–12 GB |
| High | gpt‑oss‑20b MXFP4 | ~12 GB | 16–24+ GB |

The VRAM figures are rough guides. If a model doesn't fit on the GPU, llama.cpp falls back to partial or full CPU offload. To change the models without rebuilding, edit `config/model_catalog.json`.

## Running from source

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
python CrimsonSoundtrackStudio.py                  # GUI (data goes to ./dev_home)
python CrimsonSoundtrackStudio.py --scan "D:\SteamLibrary\steamapps\common\Crimson Desert"
python CrimsonSoundtrackStudio.py --selftest       # end-to-end check on a synthetic install
python -m pytest -q                                # test suite (no game files needed)
```

To use AI features from source, put a llama.cpp build in `dev_home/runtime/llama/` (containing `llama-server(.exe)`).

Integration tests that run against a real installation are local only:
`CRIMSON_DESERT_DIR=... python -m pytest tests/test_real_game.py -m game -s`.

## Building the portable app

GitHub Actions (`.github/workflows/build-windows.yml`) runs the build on `windows-latest`:
1. Runs the tests.
2. Runs PyInstaller in one‑folder mode.
3. Bundles the llama.cpp Vulkan runtime (for AMD, NVIDIA and Intel GPUs, with CPU fallback).
4. Runs a self‑test of the frozen EXE from a copy moved to another folder.
5. Uploads `CrimsonSoundtrackStudio-<version>-windows-portable.zip`.

Tags named `v*` also publish a release.

## Documentation

- [docs/architecture.md](docs/architecture.md): layers, database, caching, output formats.
- [docs/research/](docs/research): Crimson Desert archive and Wwise knowledge, community sources and trust labels, and open questions.

## Credits and licences

The format knowledge comes from public community work: CDMW (Ratty123, MIT), crimson‑desert‑unpacker (lazorr410, MIT), CrimsonForge (hzeem, MIT), wwiser (bnnm) and vgmstream. No code was copied. The bundled runtime is [llama.cpp](https://github.com/ggml-org/llama.cpp) (MIT). Each model is under its own licence, which is shown in the app before download. No game files or model weights are included in this repository.

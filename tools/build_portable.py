"""Assemble the portable ZIP after PyInstaller has produced ``dist/CrimsonSoundtrackStudio``.

    python tools/build_portable.py [--dist dist/CrimsonSoundtrackStudio] [--out dist]

Produces ``CrimsonSoundtrackStudio-<version>-windows-portable.zip`` containing the
application folder (EXE, ``_internal/``, ``runtime/``, a README and an empty data
skeleton). Model weights are never included.
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cstudio import __version__  # noqa: E402

README = """Crimson Soundtrack Studio {version} - portable Windows build
===========================================================

1. Extract this folder anywhere you can write to (Desktop, a games drive...).
   Avoid "Program Files" - the application keeps all of its data next to the EXE.
2. Run CrimsonSoundtrackStudio.exe. Nothing needs to be installed.
3. Select your Crimson Desert folder, pick an AI tier and download a model
   (optional - the analyzer works without AI).

Everything the program creates stays inside this folder:
  data/      database, scans, knowledge, reports
  models/    downloaded local AI models
  runtime/   bundled llama.cpp runtime (MIT licence, see runtime/llama)
  cache/ logs/ output/ temp/ config/

Move or copy the whole folder and all data moves with it.
The game installation is only ever read, never modified.
"""

SKELETON = ["data", "models", "cache", "logs", "output", "temp", "config"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", default=str(ROOT / "dist" / "CrimsonSoundtrackStudio"))
    parser.add_argument("--out", default=str(ROOT / "dist"))
    parser.add_argument("--allow-missing-runtime", action="store_true")
    args = parser.parse_args()
    app = Path(args.dist)
    exe = app / "CrimsonSoundtrackStudio.exe"
    if not exe.is_file():
        print(f"{exe} not found - run PyInstaller first", file=sys.stderr)
        return 1
    if not (app / "runtime" / "llama" / "llama-server.exe").is_file() and not args.allow_missing_runtime:
        print("runtime/llama/llama-server.exe missing - run tools/fetch_llama_runtime.py", file=sys.stderr)
        return 1
    for gguf in app.rglob("*.gguf"):
        print(f"refusing to package model weights: {gguf}", file=sys.stderr)
        return 1
    (app / "README-PORTABLE.txt").write_text(README.format(version=__version__), encoding="utf-8")
    out = Path(args.out) / f"CrimsonSoundtrackStudio-{__version__}-windows-portable.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(app.rglob("*")):
            rel = Path("CrimsonSoundtrackStudio") / path.relative_to(app)
            if path.is_file():
                zf.write(path, rel.as_posix())
        for name in SKELETON:
            if not (app / name).exists():
                zf.writestr(f"CrimsonSoundtrackStudio/{name}/", "")
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

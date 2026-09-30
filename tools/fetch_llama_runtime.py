"""Download a prebuilt llama.cpp Windows runtime into the portable build.

Used by CI. The runtime is *distributed alongside* the application
(``runtime/llama/``) so users never install anything. llama.cpp is MIT
licensed; its licence is copied next to the binaries.

    python tools/fetch_llama_runtime.py --dest dist/CrimsonSoundtrackStudio/runtime/llama [--tag b11284] [--flavor vulkan]

``--tag latest`` (default) resolves the newest release. The resolved tag is
recorded in ``runtime/llama/RUNTIME_INFO.json``.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

API = "https://api.github.com/repos/ggml-org/llama.cpp/releases"
FLAVORS = {
    "vulkan": r"bin-win-vulkan-x64\.zip$",   # AMD, NVIDIA and Intel GPUs via Vulkan; CPU fallback included
    "cpu": r"bin-win-cpu-x64\.zip$",
    "hip": r"bin-win-hip[^/]*-x64\.zip$",
}


def _get(url: str) -> bytes:
    headers = {"User-Agent": "CrimsonSoundtrackStudio-build", "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=300) as resp:
        return resp.read()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dest", required=True)
    parser.add_argument("--tag", default=os.environ.get("LLAMA_CPP_TAG", "latest"))
    parser.add_argument("--flavor", default="vulkan", choices=sorted(FLAVORS))
    args = parser.parse_args()
    release = json.loads(_get(f"{API}/latest" if args.tag == "latest" else f"{API}/tags/{args.tag}"))
    pattern = re.compile(FLAVORS[args.flavor])
    assets = [a for a in release.get("assets", []) if pattern.search(a["name"])]
    if not assets:
        names = [a["name"] for a in release.get("assets", [])]
        print(f"no {args.flavor} asset in release {release.get('tag_name')}: {names}", file=sys.stderr)
        return 1
    asset = sorted(assets, key=lambda a: len(a["name"]))[0]
    print(f"llama.cpp {release['tag_name']}: downloading {asset['name']} ({asset['size'] / 1e6:.1f} MB)")
    blob = _get(asset["browser_download_url"])
    dest = Path(args.dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        zf.extractall(dest)
    # flatten a single top-level folder if the archive has one
    entries = list(dest.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        inner = entries[0]
        for child in inner.iterdir():
            shutil.move(str(child), dest / child.name)
        inner.rmdir()
    server = next(iter(dest.rglob("llama-server.exe")), None)
    if server is None:
        print("llama-server.exe not found in the downloaded archive", file=sys.stderr)
        return 1
    if server.parent != dest:
        for child in server.parent.iterdir():
            shutil.move(str(child), dest / child.name)
    try:
        licence = _get(f"https://raw.githubusercontent.com/ggml-org/llama.cpp/{release['tag_name']}/LICENSE")
        (dest / "LICENSE-llama.cpp.txt").write_bytes(licence)
    except Exception as exc:  # noqa: BLE001
        print(f"warning: could not fetch licence: {exc}", file=sys.stderr)
    (dest / "RUNTIME_INFO.json").write_text(json.dumps({
        "project": "llama.cpp", "url": "https://github.com/ggml-org/llama.cpp", "tag": release["tag_name"],
        "asset": asset["name"], "flavor": args.flavor, "license": "MIT",
    }, indent=2), encoding="utf-8")
    print(f"runtime installed in {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

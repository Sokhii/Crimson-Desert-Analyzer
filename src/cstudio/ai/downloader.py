"""Resumable, verified model downloads into ``models/<model id>/``.

Verification:

1. The expected SHA-256 and size are looked up from the Hugging Face API
   (``lfs.oid`` / ``lfs.size`` of the file) when available.
2. The downloaded file is hashed while streaming; a mismatch deletes it.
3. The file must start with the ``GGUF`` magic.

Nothing is written outside the application folder.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from cstudio import __version__
from cstudio.app_paths import AppPaths

from .catalog import LocalModel

USER_AGENT = f"CrimsonSoundtrackStudio/{__version__}"
CHUNK = 1024 * 1024


class DownloadError(Exception):
    pass


class DownloadCancelled(DownloadError):
    pass


@dataclass
class DownloadProgress:
    downloaded: int
    total: Optional[int]
    speed_bps: float
    phase: str


def _ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    try:  # frozen builds may ship certifi; the OS store is used otherwise
        import certifi  # type: ignore

        context.load_verify_locations(certifi.where())
    except Exception:  # noqa: BLE001
        pass
    return context


def _request(url: str, headers: Optional[dict] = None, method: str = "GET") -> urllib.request.Request:
    base = {"User-Agent": USER_AGENT}
    token = os.environ.get("HF_TOKEN")
    if token and "huggingface.co" in url:
        base["Authorization"] = f"Bearer {token}"
    base.update(headers or {})
    return urllib.request.Request(url, headers=base, method=method)


_SPLIT_RE = re.compile(r"-\d{5}-of-\d{5}\.gguf$", re.IGNORECASE)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def list_repo_files(repository: str, revision: str = "main", timeout: float = 20.0) -> List[dict]:
    """Files of a Hugging Face model repository (recursive). Raises DownloadError when unreachable."""

    url = f"https://huggingface.co/api/models/{repository}/tree/{revision or 'main'}?recursive=true"
    try:
        with urllib.request.urlopen(_request(url), timeout=timeout, context=_ssl_context()) as resp:
            listing = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise DownloadError(f"Hugging Face repository {repository} is not available (HTTP {exc.code})") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise DownloadError(f"could not list {repository}: {exc}") from exc
    return [item for item in listing if isinstance(item, dict) and item.get("type", "file") == "file"]


def pick_gguf(files: List[dict], filename: str, quantization: str) -> Optional[dict]:
    """Choose the model file: exact name, then case-insensitive name, then quantization match."""

    ggufs = [f for f in files if str(f.get("path", "")).lower().endswith(".gguf")
             and "mmproj" not in f["path"].lower() and not _SPLIT_RE.search(f["path"])]
    for f in ggufs:
        if f["path"] == filename:
            return f
    for f in ggufs:
        if f["path"].lower() == filename.lower() or f["path"].rsplit("/", 1)[-1].lower() == filename.lower():
            return f
    quant = _norm(quantization)
    if quant:
        matches = [f for f in ggufs if quant in _norm(f["path"].rsplit("/", 1)[-1])]
        if matches:
            return sorted(matches, key=lambda f: (f["path"].count("/"), len(f["path"])))[0]
    return None


def resolve_source(model: LocalModel) -> dict:
    """Find the real file for a catalog model on Hugging Face.

    Returns ``{"url", "repository", "path", "sha256", "size"}``. The catalog names a preferred
    repository/file; repositories rename files and use different capitalisation, so the file is
    looked up from the repository listing and alternative repositories are tried in order.
    """

    if model.source != "huggingface" or not model.repository:
        return {"url": model.resolved_url()}
    errors = []
    for repository in [model.repository] + [r for r in model.alternate_repositories if r != model.repository]:
        try:
            files = list_repo_files(repository, model.revision)
        except DownloadError as exc:
            errors.append(str(exc))
            continue
        chosen = pick_gguf(files, model.filename, model.quantization)
        if chosen is None:
            names = [f["path"] for f in files if f["path"].lower().endswith(".gguf")][:8]
            errors.append(f"{repository}: no {model.quantization or model.filename} GGUF file (has: {', '.join(names) or 'none'})")
            continue
        lfs = chosen.get("lfs") or {}
        return {
            "url": f"https://huggingface.co/{repository}/resolve/{model.revision or 'main'}/{chosen['path']}",
            "repository": repository,
            "path": chosen["path"],
            "sha256": lfs.get("oid") or lfs.get("sha256"),
            "size": lfs.get("size") or chosen.get("size"),
        }
    raise DownloadError("could not find the model file on Hugging Face:\n" + "\n".join(errors))


def fetch_expected(model: LocalModel, timeout: float = 20.0) -> dict:
    """Expected ``{"sha256", "size"}`` for verification (best effort)."""

    try:
        found = resolve_source(model)
    except DownloadError:
        return {}
    return {k: found[k] for k in ("sha256", "size") if found.get(k)}


def download_model(
    model: LocalModel,
    paths: AppPaths,
    progress: Optional[Callable[[DownloadProgress], None]] = None,
    cancel: Optional[threading.Event] = None,
    expected: Optional[dict] = None,
    url: Optional[str] = None,
) -> dict:
    cancel = cancel or threading.Event()
    source: dict = {}
    if url is None and model.source == "huggingface":
        source = resolve_source(model)
        url = source["url"]
        if expected is None:
            expected = {k: source[k] for k in ("sha256", "size") if source.get(k)}
    url = url or model.resolved_url()
    if not url:
        raise DownloadError(f"model {model.id} has no download source")
    target = model.install_path(paths)
    if not paths.is_inside(target):
        raise DownloadError("models must be stored inside the application folder")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    expected = expected if expected is not None else {}
    total = expected.get("size")

    offset = partial.stat().st_size if partial.exists() else 0
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    try:
        resp = urllib.request.urlopen(_request(url, headers), timeout=60, context=_ssl_context())
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and offset:  # already complete
            resp = None
        elif exc.code in (401, 403):
            raise DownloadError(f"access denied ({exc.code}); the model may require accepting a licence on Hugging Face") from exc
        elif exc.code == 404:
            raise DownloadError(f"the model file was not found (HTTP 404): {url}\nThe repository may have renamed or"
                                " removed it; edit config/model_catalog.json or choose another model.") from exc
        else:
            raise DownloadError(f"HTTP {exc.code} while downloading {url}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise DownloadError(f"could not reach {url}: {exc}") from exc

    if resp is not None:
        with resp:
            status = getattr(resp, "status", 200)
            if offset and status != 206:
                offset = 0  # server ignored the range request: restart
            length = resp.headers.get("Content-Length")
            if total is None and length is not None:
                total = int(length) + offset
            mode = "ab" if offset else "wb"
            done = offset
            started = time.monotonic()
            last = 0.0
            with open(partial, mode) as out:
                while True:
                    if cancel.is_set():
                        raise DownloadCancelled("download cancelled (partial file kept for resuming)")
                    block = resp.read(CHUNK)
                    if not block:
                        break
                    out.write(block)
                    done += len(block)
                    now = time.monotonic()
                    if progress and now - last > 0.25:
                        last = now
                        speed = (done - offset) / max(0.001, now - started)
                        progress(DownloadProgress(done, total, speed, "downloading"))
    if progress:
        progress(DownloadProgress(partial.stat().st_size, total, 0.0, "verifying"))
    result = verify_file(partial, expected, cancel)
    partial.replace(target)
    result["path"] = str(target)
    result["source_url"] = url
    if source.get("repository"):
        result["repository"] = source["repository"]
    if progress:
        progress(DownloadProgress(result["size"], result["size"], 0.0, "verified"))
    return result


def verify_file(path: Path, expected: Optional[dict] = None, cancel: Optional[threading.Event] = None) -> dict:
    expected = expected or {}
    size = path.stat().st_size
    with open(path, "rb") as handle:
        magic = handle.read(4)
    if magic != b"GGUF":
        path.unlink(missing_ok=True)
        raise DownloadError("downloaded file is not a GGUF model (bad magic); it was removed")
    if expected.get("size") and int(expected["size"]) != size:
        raise DownloadError(f"size mismatch: expected {expected['size']} bytes, got {size} (partial file kept for resuming)")
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled("verification cancelled")
            block = handle.read(CHUNK * 8)
            if not block:
                break
            digest.update(block)
    sha = digest.hexdigest()
    if expected.get("sha256") and expected["sha256"].lower() != sha:
        path.unlink(missing_ok=True)
        raise DownloadError("SHA-256 mismatch; the corrupt download was removed")
    return {"sha256": sha, "size": size, "hash_checked_against_source": bool(expected.get("sha256"))}

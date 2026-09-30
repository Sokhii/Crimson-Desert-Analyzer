"""Local LLM runtime abstraction.

``InferenceBackend`` is the only interface the investigation agent (and the
future music-matching layer) depends on. Implementations:

* ``LlamaServerBackend`` - launches the bundled ``llama-server`` (llama.cpp)
  from ``runtime/`` on 127.0.0.1 and talks to its OpenAI-compatible API. No
  external AI application is involved; the process is owned by this app and
  stopped with it.
* ``OpenAICompatibleBackend`` - talks to an already running compatible server
  (advanced/developer option; never required).
* ``ScriptedBackend`` - deterministic replies for tests.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from cstudio.app_paths import AppPaths

from .catalog import LocalModel


class BackendError(RuntimeError):
    pass


class InferenceBackend:
    name = "abstract"

    def start(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def stop(self) -> None:  # pragma: no cover - interface
        pass

    def is_ready(self) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def chat(self, messages: List[Dict[str, str]], *, json_schema: Optional[dict] = None, max_tokens: int = 1024,
             temperature: float = 0.2) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def describe(self) -> Dict[str, Any]:
        return {"backend": self.name}


def _post_json(url: str, payload: dict, timeout: float) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never proxy localhost traffic
    try:
        with opener.open(request, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:800]
        raise BackendError(f"HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise BackendError(str(exc)) from exc


class OpenAICompatibleBackend(InferenceBackend):
    name = "openai-compatible"

    def __init__(self, base_url: str, model_name: str = "local", timeout: float = 600.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model_name = model_name
        self.timeout = timeout
        self.schema_supported = True

    def start(self) -> None:
        if not self.is_ready():
            raise BackendError(f"no server answering at {self.base_url}")

    def is_ready(self) -> bool:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for path in ("/health", "/v1/models"):
            try:
                with opener.open(self.base_url + path, timeout=3) as resp:
                    if resp.status == 200:
                        return True
            except (urllib.error.URLError, OSError):
                continue
        return False

    def chat(self, messages, *, json_schema=None, max_tokens=1024, temperature=0.2) -> str:
        payload: Dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if json_schema is not None and self.schema_supported:
            payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "action", "schema": json_schema, "strict": True}}
        try:
            reply = _post_json(self.base_url + "/v1/chat/completions", payload, self.timeout)
        except BackendError as exc:
            if json_schema is not None and self.schema_supported and "response_format" in str(exc).lower():
                self.schema_supported = False
                payload.pop("response_format", None)
                reply = _post_json(self.base_url + "/v1/chat/completions", payload, self.timeout)
            else:
                raise
        try:
            message = reply["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise BackendError(f"unexpected reply: {str(reply)[:300]}") from exc
        content = message.get("content") or ""
        if not content.strip() and message.get("reasoning_content"):
            content = message["reasoning_content"]
        return content

    def describe(self) -> Dict[str, Any]:
        return {"backend": self.name, "url": self.base_url}


def find_llama_server(paths: AppPaths, override: str = "") -> Optional[Path]:
    exe = "llama-server.exe" if os.name == "nt" else "llama-server"
    if override:
        candidate = Path(override)
        if not candidate.is_absolute():
            candidate = paths.root / candidate
        return candidate if candidate.is_file() else None
    for sub in ("llama", "llama-vulkan", "llama-cpu", ""):
        candidate = paths.runtime / sub / exe if sub else paths.runtime / exe
        if candidate.is_file():
            return candidate
    for candidate in sorted(paths.runtime.glob(f"**/{exe}")):
        return candidate
    return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class LlamaServerBackend(OpenAICompatibleBackend):
    """Owns a ``llama-server`` child process bound to localhost."""

    name = "llama.cpp"

    def __init__(self, paths: AppPaths, model: LocalModel, *, server_path: Optional[Path] = None, gpu_layers: str = "auto",
                 context_size: int = 0, threads: int = 0, log: Optional[Callable[[str], None]] = None) -> None:
        self.paths = paths
        self.model = model
        self.server_path = server_path or find_llama_server(paths)
        self.gpu_layers = str(gpu_layers or "auto")
        self.context_size = context_size or model.context_size or 8192
        self.threads = threads
        self.log = log or (lambda _m: None)
        self.port = _free_port()
        super().__init__(f"http://127.0.0.1:{self.port}", model_name=model.id)
        self.process: Optional[subprocess.Popen] = None
        self.log_file = paths.logs / "llama-server.log"
        self.active_args: List[str] = []
        self._lock = threading.Lock()

    def _attempts(self) -> Iterable[List[str]]:
        model_path = self.model.install_path(self.paths)
        base = ["-m", str(model_path), "--host", "127.0.0.1", "--port", str(self.port), "-c", str(self.context_size),
                "--jinja", "--no-webui"]
        if self.threads:
            base += ["-t", str(self.threads)]
        if self.gpu_layers == "auto":
            yield base + ["-ngl", "999"]           # everything on the GPU when it fits
            yield base + ["-ngl", "20"]            # partial offload
            yield base + ["-ngl", "0"]             # CPU only
        else:
            yield base + ["-ngl", self.gpu_layers]
            if self.gpu_layers != "0":
                yield base + ["-ngl", "0"]

    def start(self, timeout: float = 600.0) -> None:
        with self._lock:
            if self.process and self.process.poll() is None and self.is_ready():
                return
            if self.server_path is None or not self.server_path.is_file():
                raise BackendError("the bundled llama.cpp runtime (runtime/llama/llama-server.exe) was not found")
            model_path = self.model.install_path(self.paths)
            if not model_path.is_file():
                raise BackendError(f"model file not found: {model_path}")
            errors = []
            for args in self._attempts():
                for drop_webui in (False, True):
                    attempt = [a for a in args if not (drop_webui and a == "--no-webui")]
                    ok, message = self._launch(attempt, timeout)
                    if ok:
                        self.active_args = attempt
                        return
                    errors.append(message)
                    if "--no-webui" not in message and "unknown" not in message.lower() and "invalid argument" not in message.lower():
                        break
            raise BackendError("llama-server failed to start:\n" + "\n".join(errors[-3:]))

    def _launch(self, args: List[str], timeout: float) -> tuple:
        self.stop()
        self.paths.logs.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["LLAMA_CACHE"] = str(self.paths.cache / "llama")  # keep any llama.cpp cache inside the app folder
        creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        log_handle = open(self.log_file, "ab")
        cmd = [str(self.server_path)] + args
        self.log("starting: " + " ".join(cmd))
        log_handle.write(("\n==== " + time.strftime("%Y-%m-%d %H:%M:%S") + " " + " ".join(cmd) + "\n").encode())
        log_handle.flush()
        try:
            self.process = subprocess.Popen(
                cmd, cwd=str(self.server_path.parent), stdout=log_handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                env=env, creationflags=creationflags,
            )
        except OSError as exc:
            log_handle.close()
            return False, f"could not execute {self.server_path}: {exc}"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                log_handle.close()
                return False, f"exited with code {self.process.returncode}: {self._log_tail()}"
            if self.is_ready():
                log_handle.close()
                return True, ""
            time.sleep(0.5)
        log_handle.close()
        self.stop()
        return False, "timed out waiting for the model to load"

    def _log_tail(self, lines: int = 12) -> str:
        try:
            text = self.log_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(text.strip().splitlines()[-lines:])

    def is_ready(self) -> bool:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(self.base_url + "/health", timeout=2) as resp:
                return resp.status == 200
        except (urllib.error.URLError, OSError):
            return False

    def stop(self) -> None:
        proc = self.process
        self.process = None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    def describe(self) -> Dict[str, Any]:
        return {"backend": self.name, "server": str(self.server_path), "model": self.model.id, "args": self.active_args,
                "log": str(self.log_file)}


class ScriptedBackend(InferenceBackend):
    """Replays a list of responses (strings or callables of the message list)."""

    name = "scripted"

    def __init__(self, responses: List[Any]) -> None:
        self.responses = list(responses)
        self.calls: List[List[Dict[str, str]]] = []

    def start(self) -> None:
        pass

    def is_ready(self) -> bool:
        return True

    def chat(self, messages, *, json_schema=None, max_tokens=1024, temperature=0.2) -> str:
        self.calls.append(messages)
        if not self.responses:
            return json.dumps({"thought": "nothing left", "tool": "stop_investigation", "args": {}})
        item = self.responses.pop(0)
        return item(messages) if callable(item) else item


def run_inference_check(backend: InferenceBackend) -> Dict[str, Any]:
    started = time.monotonic()
    reply = backend.chat(
        [{"role": "system", "content": "You are a terse assistant."},
         {"role": "user", "content": 'Answer with JSON {"ok": true, "word": "<any word>"}.'}],
        json_schema={"type": "object", "properties": {"ok": {"type": "boolean"}, "word": {"type": "string"}},
                     "required": ["ok", "word"]},
        max_tokens=64,
        temperature=0.0,
    )
    elapsed = time.monotonic() - started
    ok = bool(reply and reply.strip())
    return {"ok": ok, "reply": reply[:200], "seconds": round(elapsed, 2)}


def platform_note() -> str:
    return f"{sys.platform} / python {sys.version.split()[0]}"

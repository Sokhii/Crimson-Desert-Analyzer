import hashlib
import json
import os
import stat
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from cstudio.ai.agent import Investigator, build_tasks, parse_action
from cstudio.ai.catalog import LocalModel, ModelCatalog, register_custom_model
from cstudio.ai.downloader import DownloadError, download_model, verify_file
from cstudio.ai.runtime import LlamaServerBackend, ScriptedBackend, run_inference_check
from cstudio.ai.tools import InvestigationTools
from cstudio.analyzer.scanner import Scanner
from cstudio.config import ensure_default_config
from cstudio.knowledge.store import KnowledgeStore


def J(tool, args, thought="t"):
    return json.dumps({"thought": thought, "tool": tool, "args": args})


@pytest.fixture
def tools(scanned, db, app_paths):
    result, _info, _root = scanned
    t = InvestigationTools(db, app_paths, result.installation_id, KnowledgeStore(db, app_paths), "test-model", None)
    yield t
    t.close()


def test_catalog_tiers(app_paths, db):
    ensure_default_config(app_paths)
    catalog = ModelCatalog.load(app_paths, db)
    for tier in ("low", "medium", "high"):
        model = catalog.default_for_tier(tier)
        assert model is not None and model.tier == tier
        assert model.resolved_url().startswith("https://huggingface.co/")
        assert app_paths.is_inside(model.install_path(app_paths))
    assert catalog.get("gpt-oss-20b-mxfp4").recommended_vram_gb == 16


def test_custom_model(app_paths, db, tmp_path):
    gguf = app_paths.models / "mine.gguf"
    gguf.write_bytes(b"GGUF" + b"\x00" * 100)
    model = register_custom_model(db, app_paths, gguf)
    assert ModelCatalog.load(app_paths, db).get(model.id).install_path(app_paths) == gguf
    bad = tmp_path / "bad.gguf"
    bad.write_bytes(b"nope")
    with pytest.raises(ValueError):
        register_custom_model(db, app_paths, bad)


def _serve(directory):
    handler = partial(SimpleHTTPRequestHandler, directory=str(directory))
    handler.log_message = lambda *a: None
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_download_and_verify(app_paths, tmp_path, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    blob = b"GGUF" + os.urandom(3 * 1024 * 1024)
    (tmp_path / "srv").mkdir()
    (tmp_path / "srv" / "m.gguf").write_bytes(blob)
    server = _serve(tmp_path / "srv")
    url = f"http://127.0.0.1:{server.server_address[1]}/m.gguf"
    model = LocalModel(id="t", display_name="t", tier="low", source="url", filename="m.gguf")
    try:
        expected = {"sha256": hashlib.sha256(blob).hexdigest(), "size": len(blob)}
        result = download_model(model, app_paths, url=url, expected=expected)
        assert result["sha256"] == expected["sha256"]
        assert (app_paths.models / "t" / "m.gguf").read_bytes() == blob
        bad = LocalModel(id="bad", display_name="b", tier="low", source="url", filename="m.gguf")
        with pytest.raises(DownloadError):
            download_model(bad, app_paths, url=url, expected={"sha256": "0" * 64, "size": len(blob)})
        assert not (app_paths.models / "bad" / "m.gguf").exists()
    finally:
        server.shutdown()


def test_verify_rejects_non_gguf(tmp_path):
    f = tmp_path / "x.gguf"
    f.write_bytes(b"HTML error page")
    with pytest.raises(DownloadError):
        verify_file(f)


def test_tools_read_only_and_useful(tools, fake_game):
    root, _ = fake_game
    before = sorted((p.name, p.stat().st_mtime_ns) for p in root.rglob("*"))
    assert tools.call("search_files", {"pattern": "bgm"})["matches"]
    assert "hex" in tools.call("inspect_file", {"path": "0004/sound/bgm.bnk", "length": 64})
    assert tools.call("inspect_bnk", {"bank": "bgm"})["version"] == 150
    obj = tools.call("inspect_object_bytes", {"object_id": 9001})
    assert obj["parse_status"] == "header_only" and "hex" in obj
    assert tools.call("get_wem_info", {"source_id": 433831842})["role"] == "music"
    assert tools.call("get_container", {"object_id": 4001})["nodes"]
    assert tools.call("hash_name", {"name": "bgm"})["exists_in_scan"] is True
    assert tools.call("test_names", {"names": ["nope_x", "Play_BGM_World"]})["matches"][0]["name"] == "Play_BGM_World"
    assert tools.call("extract_strings", {"path": "0004/gamedata/musicinfo.pabgb", "contains": "BGM"})["count"] >= 1
    assert tools.call("get_unknown_structures", {})["unknown_structures"]
    assert "error" in tools.call("inspect_file", {"path": "loose:../../etc/passwd"})
    assert "error" in tools.call("no_such_tool", {})
    assert "error" in tools.call("get_object", {"object_id": "abc"})
    assert sorted((p.name, p.stat().st_mtime_ns) for p in root.rglob("*")) == before


def test_verified_requires_deterministic_check(tools):
    rec = tools.call("record_finding", {"title": "t", "statement": "s", "status": "verified", "evidence": ["one", "two"]})
    assert rec["status"] == "probable"
    weak = tools.call("record_finding", {"title": "t2", "statement": "s2", "status": "probable", "evidence": ["one"]})
    assert weak["status"] == "hypothesis"
    fail = tools.call("mark_verified", {"uid": rec["recorded"], "check": {"type": "name_hash", "name": "wrong", "id": 412724365}})
    assert fail["verified"] is False
    assert tools.knowledge.get(rec["recorded"])["status"] == "probable"
    ok = tools.call("mark_verified", {"uid": rec["recorded"], "check": {"type": "name_hash", "name": "bgm", "id": 412724365}})
    assert ok["verified"] is True
    assert tools.knowledge.get(rec["recorded"])["status"] == "verified"
    assert tools.knowledge.verified_names()[412724365] == "bgm"
    bytes_check = tools.call("mark_verified", {"uid": weak["recorded"], "check": {"type": "bytes_match", "path": "0004/sound/bgm.bnk",
                                                                                   "offset": 0, "hex": "424b4844"}})
    assert bytes_check["verified"] is True


def test_knowledge_files_and_sync(tools, db, app_paths):
    rec = tools.call("record_hypothesis", {"title": "h", "statement": "maybe", "evidence": "x"})
    uid = rec["recorded"]
    assert (app_paths.knowledge_hypotheses / f"{uid}.json").is_file()
    db.execute("DELETE FROM finding")
    db.commit()
    assert KnowledgeStore(db, app_paths).sync_from_files() >= 1
    assert KnowledgeStore(db, app_paths).get(uid)["statement"] == "maybe"


def test_parse_action_variants():
    assert parse_action('{"thought":"a","tool":"x","args":{}}')["tool"] == "x"
    assert parse_action('```json\n{"tool":"y"}\n```')["args"] == {}
    assert parse_action('<think>hmm</think> sure: {"thought":"b","tool":"z","args":{"a":1}} trailing')["args"] == {"a": 1}
    assert parse_action("no json here") is None


def test_agent_iterative_run_and_knowledge_reuse(scanned, db, app_paths):
    result, _info, root = scanned
    inst = result.installation_id
    tasks = build_tasks(db, inst)
    assert any(t.subject == "hirc_type:v150:0x2A" for t in tasks)
    tasks = [t for t in tasks if t.subject == "hirc_type:v150:0x2A"]
    script = [
        "garbage that is not json",
        J("inspect_object_bytes", {"object_id": 9001}),
        J("find_references", {"id": 4001}),
        J("record_finding", {"title": "0x2A links to the world music switch", "statement": "object 9001 stores id 4001",
                             "status": "probable", "category": "unknown_structure_explanation",
                             "subject_type": "unknown_structure", "subject_key": "hirc_type:v150:0x2A",
                             "evidence": ["u32 at +4 is 4001", "4001 is a MusicSwitchContainer"]}),
        J("finish_task", {"summary": "done"}),
    ]
    backend = ScriptedBackend(script)
    progress = []
    state = Investigator(db, app_paths, inst, backend, "scripted", progress=lambda p: progress.append(p.steps)).run(tasks)
    assert state.status == "completed" and state.steps == 4
    assert db.scalar("SELECT COUNT(*) FROM ai_step") == 4
    assert progress
    again = Scanner(db, app_paths).run(root)
    row = db.query_one("SELECT status FROM unknown_structure WHERE signature='hirc_type:v150:0x2A'")
    assert row["status"] == "explained"
    assert again.stats.ai_findings >= 1


def test_agent_stop_and_pause(scanned, db, app_paths):
    result, _info, _root = scanned
    stop = threading.Event()
    calls = {"n": 0}

    def reply(_messages):
        calls["n"] += 1
        if calls["n"] == 2:
            stop.set()
        return J("list_findings", {})

    backend = ScriptedBackend([reply] * 50)
    state = Investigator(db, app_paths, result.installation_id, backend, "s", stop=stop, max_steps_per_task=50).run()
    assert state.status == "stopped" and state.steps <= 3


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX shebang launcher")
def test_llama_server_backend_with_fake_server(app_paths, tmp_path):
    exe = app_paths.runtime / "llama" / "llama-server"
    exe.parent.mkdir(parents=True)
    script = Path(__file__).with_name("fake_llama_server.py")
    exe.write_text(f"#!/bin/sh\nexec '{sys.executable}' '{script}' \"$@\"\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    model_file = app_paths.models / "m" / "m.gguf"
    model_file.parent.mkdir(parents=True)
    model_file.write_bytes(b"GGUF --fail-gpu" + b"\x00" * 64)
    model = LocalModel(id="m", display_name="m", tier="low", filename="m.gguf")
    backend = LlamaServerBackend(app_paths, model)
    try:
        backend.start(timeout=30)
        assert backend.active_args[backend.active_args.index("-ngl") + 1] == "0"  # fell back to CPU
        assert run_inference_check(backend)["ok"]
    finally:
        backend.stop()
    assert "out of device memory" in (app_paths.logs / "llama-server.log").read_text()


def test_pick_gguf_by_name_case_and_quantization():
    from cstudio.ai.downloader import pick_gguf

    files = [{"path": "README.md"}, {"path": "gpt-oss-20b-MXFP4.gguf", "lfs": {"oid": "a" * 64, "size": 5}},
             {"path": "mmproj-F16.gguf"}, {"path": "big/model-Q4_K_M-00001-of-00002.gguf"},
             {"path": "Qwen3-8B-Q4_K_M.gguf"}, {"path": "Qwen3-8B-Q8_0.gguf"}]
    assert pick_gguf(files, "gpt-oss-20b-mxfp4.gguf", "MXFP4")["path"] == "gpt-oss-20b-MXFP4.gguf"
    assert pick_gguf(files, "renamed.gguf", "Q4_K_M")["path"] == "Qwen3-8B-Q4_K_M.gguf"
    assert pick_gguf(files, "gpt-oss-20b.MXFP4.gguf", "mxfp4")["path"] == "gpt-oss-20b-MXFP4.gguf"
    assert pick_gguf(files, "nothing.gguf", "IQ2_XS") is None


def test_resolve_source_falls_back_to_alternate_repo(monkeypatch):
    from cstudio.ai import downloader

    listings = {
        "ggml-org/gpt-oss-20b-GGUF": [{"path": "README.md"}],
        "lmstudio-community/gpt-oss-20b-GGUF": [{"path": "gpt-oss-20b-MXFP4.gguf", "lfs": {"oid": "b" * 64, "size": 12}}],
    }

    def fake_list(repo, revision="main", timeout=20.0):
        if repo not in listings:
            raise downloader.DownloadError(f"{repo} missing")
        return listings[repo]

    monkeypatch.setattr(downloader, "list_repo_files", fake_list)
    model = LocalModel(id="g", display_name="g", tier="high", repository="ggml-org/gpt-oss-20b-GGUF",
                       filename="gpt-oss-20b-mxfp4.gguf", quantization="MXFP4",
                       alternate_repositories=["missing/repo", "lmstudio-community/gpt-oss-20b-GGUF"])
    found = downloader.resolve_source(model)
    assert found["url"] == "https://huggingface.co/lmstudio-community/gpt-oss-20b-GGUF/resolve/main/gpt-oss-20b-MXFP4.gguf"
    assert found["sha256"] == "b" * 64 and found["size"] == 12
    model.alternate_repositories = []
    with pytest.raises(downloader.DownloadError) as info:
        downloader.resolve_source(model)
    assert "README.md" not in str(info.value) and "no MXFP4" in str(info.value)


def test_catalog_models_have_alternates():
    from cstudio.config import RESOURCES

    data = json.loads((RESOURCES / "model_catalog.json").read_text())
    for m in data["models"]:
        assert m["alternate_repositories"] and m["quantization"]


def test_old_catalog_copy_is_upgraded(app_paths):
    from cstudio.config import RESOURCES

    app_paths.model_catalog_file.write_text(json.dumps({"catalog_version": 1, "models": [], "tiers": {}}))
    ensure_default_config(app_paths)
    upgraded = json.loads(app_paths.model_catalog_file.read_text())
    assert upgraded["catalog_version"] == json.loads((RESOURCES / "model_catalog.json").read_text())["catalog_version"]
    assert (app_paths.config / "model_catalog.v1.bak.json").is_file()
    ensure_default_config(app_paths)  # idempotent once current
    assert not (app_paths.config / "model_catalog.v2.bak.json").exists()

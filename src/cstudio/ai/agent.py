"""Autonomous, iterative investigation agent.

The agent works through a queue of tasks derived from the deterministic scan
(open unknown structures, unnamed banks and music, orphan/missing media...).
For each task it loops::

    model proposes {thought, tool, args}  ->  tool runs  ->  result fed back

until the model calls ``finish_task`` or the per-task step budget is spent.
Output is constrained with a JSON schema (llama.cpp grammar), which makes even
small local models produce valid tool calls. Every step is stored in
``ai_step`` so the user can audit what the model looked at.

The model never decides what is *true*: findings it records are hypotheses or
probable at best; ``verified`` requires ``mark_verified`` with a passing
deterministic check.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional

from cstudio.analyzer import queries
from cstudio.app_paths import AppPaths
from cstudio.db.database import Database, dumps, now_iso
from cstudio.knowledge.store import KnowledgeStore

from .runtime import BackendError, InferenceBackend
from .tools import InvestigationTools

SYSTEM_PROMPT = """You are the reverse-engineering investigator inside Crimson Soundtrack Studio.
You study the audio data of the game Crimson Desert (Wwise soundbanks .bnk, media .wem, SoundbanksInfo XML,
Pearl Abyss PAZ/PAMT archives). A deterministic parser has already scanned the game; its database is the source
of truth. Your job is to investigate what the parser could not explain, using the tools, and to record evidence.

Rules:
- Reply with exactly one JSON object: {"thought": "...", "tool": "<tool name>", "args": {...}}.
- Use tools to gather evidence before recording anything. Never invent ids, names or offsets.
- Wwise ids of named objects are FNV-1 hashes of the lowercase name: use hash_name/test_names to test name guesses.
- Record claims with record_hypothesis or record_finding (status probable needs >= 2 independent evidence items).
- Only mark_verified can make something verified, and only with a deterministic check that passes.
- For an unknown structure you explained, use category "unknown_structure_explanation" and subject_key = its signature.
- Do not classify gameplay context (boss/town/combat...). Focus on what assets exist, how they connect, and evidence.
- Keep thoughts short. When the task is done (or you are stuck), call finish_task with a summary.

Tools:
{tools}
"""

ACTION_SCHEMA_BASE = {
    "type": "object",
    "properties": {
        "thought": {"type": "string"},
        "tool": {"type": "string"},
        "args": {"type": "object"},
    },
    "required": ["thought", "tool", "args"],
}


@dataclass
class InvestigationTask:
    kind: str
    title: str
    prompt: str
    subject: str = ""


@dataclass
class AgentProgress:
    status: str = "idle"
    session_id: Optional[int] = None
    model: str = ""
    current_task: str = ""
    task_index: int = 0
    task_total: int = 0
    steps: int = 0
    last_thought: str = ""
    last_tool: str = ""
    last_result: str = ""
    counts: Dict[str, int] = field(default_factory=dict)
    unknowns_remaining: int = 0
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def parse_action(text: str) -> Optional[Dict[str, Any]]:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    candidates = [text]
    start = text.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start:i + 1])
                    break
    for cand in candidates:
        try:
            data = json.loads(cand)
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("tool"), str):
            if not isinstance(data.get("args"), dict):
                data["args"] = {}
            data.setdefault("thought", "")
            return data
    return None


def build_tasks(db: Database, inst_id: int, limit: int = 60) -> List[InvestigationTask]:
    tasks: List[InvestigationTask] = []
    for u in queries.unknown_structures(db, inst_id, "open"):
        examples = json.dumps((u.get("details") or {}).get("examples", [])[:4], default=str)
        tasks.append(InvestigationTask(
            "unknown_structure", f"Explain unknown structure {u['signature']}",
            f"Unknown structure signature: {u['signature']}\nCategory: {u['category']}\nOccurrences: {u['occurrences']}\n"
            f"Description: {u['description']}\nExamples: {examples}\n"
            "Investigate what this is (inspect the bytes/objects/files involved, compare examples, check references),"
            " then record a hypothesis or finding with evidence (category unknown_structure_explanation, subject_key = signature).",
            u["signature"]))
    unnamed_banks = [b for b in queries.soundbanks(db, inst_id) if not b["name"]]
    for b in unnamed_banks[:10]:
        tasks.append(InvestigationTask(
            "bank_name", f"Find the name of bank {b['bank_id']}",
            f"Bank {b['path']} has id {b['bank_id']} but no known name. Bank ids are FNV-1 hashes of the bank name."
            " Use its contents, events, file names and search_files/extract_strings to propose names and test them with"
            " test_names. If a name matches, record_finding (category name_mapping, subject_type name, subject_key the id)"
            " then mark_verified with a name_hash check.", str(b["bank_id"])))
    music = queries.media_summary_rows(db, inst_id, role="music*", limit=200)
    unnamed_music = [m for m in music if not m["name"]]
    if unnamed_music:
        ids = ", ".join(str(m["source_id"]) for m in unnamed_music[:12])
        tasks.append(InvestigationTask(
            "music_context", "Gather context for unnamed music media",
            f"These media ids are classified as music but have no name: {ids}. Use get_wem_info, get_container and"
            " find_references to describe how they are organised (segments, playlists, switch containers, events) and"
            " record findings about their structure and role evidence (no gameplay taxonomy).", "music"))
    switches = db.query(
        "SELECT o.object_id FROM wwise_object o JOIN asset a ON a.id=o.bank_asset_id WHERE a.installation_id=? AND o.type_code=12 LIMIT 5",
        (inst_id,))
    for s in switches:
        tasks.append(InvestigationTask(
            "music_switch", f"Understand music switch container {s['object_id']}",
            f"Music switch container {s['object_id']} selects music by state/switch. Inspect it (get_object, get_container),"
            " name its switch groups/states with test_names if possible, and record what drives the music selection.",
            str(s["object_id"])))
    return tasks[:limit]


class Investigator:
    def __init__(self, db: Database, paths: AppPaths, inst_id: int, backend: InferenceBackend, model_id: str,
                 progress: Optional[Callable[[AgentProgress], None]] = None, stop: Optional[threading.Event] = None,
                 pause: Optional[threading.Event] = None, max_steps_per_task: int = 14, max_total_steps: int = 400,
                 history_window: int = 8) -> None:
        self.db = db
        self.paths = paths
        self.inst_id = inst_id
        self.backend = backend
        self.model_id = model_id
        self.progress_cb = progress
        self.stop = stop or threading.Event()
        self.pause = pause or threading.Event()
        self.max_steps_per_task = max_steps_per_task
        self.max_total_steps = max_total_steps
        self.history_window = history_window
        self.knowledge = KnowledgeStore(db, paths)
        self.state = AgentProgress(model=model_id)

    def _emit(self) -> None:
        self.state.counts = self.knowledge.counts()
        self.state.unknowns_remaining = int(self.db.scalar(
            "SELECT COUNT(*) FROM unknown_structure WHERE installation_id=? AND status='open'", (self.inst_id,), 0))
        if self.progress_cb:
            self.progress_cb(self.state)

    def _wait_if_paused(self) -> None:
        while self.pause.is_set() and not self.stop.is_set():
            self.state.status = "paused"
            self._emit()
            time.sleep(0.3)

    def run(self, tasks: Optional[List[InvestigationTask]] = None) -> AgentProgress:
        tasks = tasks if tasks is not None else build_tasks(self.db, self.inst_id)
        cur = self.db.execute("INSERT INTO ai_session(model_id, started_at, status) VALUES (?,?,?)",
                              (self.model_id, now_iso(), "running"))
        session_id = int(cur.lastrowid)
        self.db.commit()
        self.state.session_id = session_id
        self.state.task_total = len(tasks)
        tools = InvestigationTools(self.db, self.paths, self.inst_id, self.knowledge, self.model_id, session_id)
        schema = dict(ACTION_SCHEMA_BASE)
        schema["properties"] = dict(schema["properties"])
        schema["properties"]["tool"] = {"type": "string", "enum": list(tools.specs)}
        system = SYSTEM_PROMPT.replace("{tools}", tools.describe())
        summaries: List[str] = []
        status = "completed"
        try:
            self.state.status = "investigating"
            self._emit()
            for index, task in enumerate(tasks):
                if self.stop.is_set() or self.state.steps >= self.max_total_steps:
                    break
                self.state.task_index = index + 1
                self.state.current_task = task.title
                self._emit()
                result = self._run_task(task, tools, system, schema, session_id)
                summaries.append(f"- {task.title}: {result}")
                if result == "STOP":
                    break
            if self.stop.is_set():
                status = "stopped"
        except BackendError as exc:
            status = "failed"
            self.state.message = f"model backend error: {exc}"
        finally:
            tools.close()
            summary = "\n".join(summaries)
            self.db.execute("UPDATE ai_session SET ended_at=?, status=?, steps=?, summary=? WHERE id=?",
                            (now_iso(), status, self.state.steps, summary, session_id))
            self.db.commit()
            self.knowledge.write_research_note(
                f"ai-session-{session_id}",
                f"# AI investigation session {session_id}\n\nModel: {self.model_id}\nStatus: {status}\n"
                f"Steps: {self.state.steps}\n\n## Tasks\n{summary}\n")
            self.state.status = status
            self._emit()
        return self.state

    def _run_task(self, task: InvestigationTask, tools: InvestigationTools, system: str, schema: dict, session_id: int) -> str:
        history: List[Dict[str, str]] = []
        failures = 0
        for _ in range(self.max_steps_per_task):
            self._wait_if_paused()
            if self.stop.is_set() or self.state.steps >= self.max_total_steps:
                return "interrupted"
            messages = [{"role": "system", "content": system},
                        {"role": "user", "content": f"TASK: {task.title}\n\n{task.prompt}\n\nStart investigating."}]
            messages += history[-self.history_window * 2:]
            raw = self.backend.chat(messages, json_schema=schema, max_tokens=700, temperature=0.2)
            action = parse_action(raw)
            if action is None:
                failures += 1
                history.append({"role": "assistant", "content": raw[:500]})
                history.append({"role": "user", "content": 'Invalid reply. Answer with ONE JSON object {"thought":...,"tool":...,"args":{...}}.'})
                if failures >= 3:
                    return "abandoned (invalid replies)"
                continue
            tool, args = action["tool"], action.get("args") or {}
            self.state.steps += 1
            self.state.last_thought = str(action.get("thought", ""))[:400]
            self.state.last_tool = f"{tool}({json.dumps(args, default=str)[:200]})"
            result = tools.call(tool, args)
            text = tools.truncate(result)
            self.state.last_result = text[:400]
            self.db.execute(
                "INSERT INTO ai_step(session_id, step_no, thought, tool, args_json, result_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (session_id, self.state.steps, self.state.last_thought, tool, dumps(args), text, now_iso()))
            self.db.execute("UPDATE ai_session SET steps=? WHERE id=?", (self.state.steps, session_id))
            self.db.commit()
            self._emit()
            if tool == "finish_task":
                return str(args.get("summary", "finished"))[:300]
            if tool == "stop_investigation":
                return "STOP"
            history.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)[:1200]})
            history.append({"role": "user", "content": f"TOOL RESULT ({tool}): {text}"})
        return "step budget used"

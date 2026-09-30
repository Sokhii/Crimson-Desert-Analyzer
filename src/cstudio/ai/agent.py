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
- Record only NEW conclusions. Do not record facts the tools already report (types, tempo, container chains).
- An unknown structure stays open until mark_verified passes; a probable explanation does not close it.
- Keep thoughts short. When the task is done (or you are stuck), call finish_task with a summary.

Wwise facts (use them, do not contradict them):
- MusicTrack plays WEM clips; MusicSegment is a timed section (duration, entry/exit markers) holding tracks;
  MusicRandomSequenceContainer is a playlist of segments; MusicSwitchContainer picks a child with a decision tree
  keyed by its "arguments" (state groups or switch groups).
- RTPC references are parameter curves (volume, filter, pitch...). They do NOT select which music plays.
- Events hold actions. Play/Stop actions target an object; SetState/SetSwitch actions change the group values
  that switch containers read.
- All HIRC object types in these banks that matter for music are fully decoded; "header_only" types are effects,
  attenuations and modulators.

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
    invalid_replies: int = 0
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


MUSIC_TYPES = ("MusicSegment", "MusicTrack", "MusicSwitchContainer", "MusicRandomSequenceContainer")
# unknown-structure signatures that concern the music hierarchy
MUSIC_UNKNOWN_PREFIXES = ("dangling_ref:transition_segment", "dangling_ref:stinger_segment", "dangling_ref:switch_assoc",
                          "dangling_ref:playlist_segment")


def _music_banks(db: Database, inst_id: int) -> List[dict]:
    """Banks holding MusicTracks, most music first, with structural twins grouped."""

    banks = [b for b in queries.soundbanks(db, inst_id) if b["type_counts"].get("MusicTrack")]
    banks.sort(key=lambda b: (-b["type_counts"].get("MusicTrack", 0), b["path"]))
    return banks


def _music_events(db: Database, inst_id: int) -> Dict[int, List[int]]:
    """event id -> music objects its actions target (Play/Stop/Seek... on the music hierarchy)."""

    from cstudio.analyzer.relationships import load_graph

    g = load_graph(db, inst_id)
    out: Dict[int, List[int]] = {}
    for event, actions in g.event_actions.items():
        targets = sorted({t for a in actions for t in g.action_targets.get(a, ()) if g.types.get(t, "") in MUSIC_TYPES})
        if targets:
            out[event] = targets
    return out


def build_tasks(db: Database, inst_id: int, limit: int = 60) -> List[InvestigationTask]:
    """Music-relevant questions first, then the remaining open unknowns.

    Bank *names* are deliberately not a goal: identification works on IDs. Tasks carry the concrete IDs the model
    needs so it does not have to hunt for them.
    """

    tasks: List[InvestigationTask] = []
    music_banks = _music_banks(db, inst_id)
    # twins must match in file size as well as structure; equal object counts alone pair unrelated small banks
    signature = lambda b: (tuple(sorted(b["type_counts"].items())), b["embedded_media"], b["object_count"], b["size"])  # noqa: E731
    groups: Dict[tuple, List[dict]] = {}
    for b in music_banks:
        groups.setdefault(signature(b), []).append(b)
    for twins in groups.values():
        if len(twins) > 1:
            paths = ", ".join(t["path"] for t in twins)
            tasks.append(InvestigationTask(
                "music_bank_twins", f"Explain duplicated music banks ({len(twins)} banks)",
                f"These banks have identical size, object and media counts: {paths}. Compare them with compare_files"
                " (it reports differing byte ranges plus a chunk-by-chunk and object-by-object comparison for banks)."
                " Record exactly what differs (header fields, which objects, which media) and what is shared, with the"
                " evidence. A replacement mod may need to patch every copy, so this matters.", ",".join(str(t["bank_id"]) for t in twins)))
    music_events = _music_events(db, inst_id)
    by_bank: Dict[str, List[int]] = {}
    object_banks = {}
    for r in db.query(
        "SELECT o.object_id, a.vpath FROM wwise_object o JOIN asset a ON a.id=o.bank_asset_id WHERE a.installation_id=?"
        " AND o.type_code IN (10,11,12,13)", (inst_id,)):
        object_banks.setdefault(r["object_id"], set()).add(r["vpath"])
    for event, targets in music_events.items():
        for t in targets:
            for path in object_banks.get(t, ()):
                by_bank.setdefault(path, [])
                if event not in by_bank[path]:
                    by_bank[path].append(event)
    for b in music_banks[:6]:
        events = by_bank.get(b["path"], [])
        if not events:
            continue
        ids = ", ".join(str(e) for e in events[:15])
        tasks.append(InvestigationTask(
            "music_events", f"How is the music in {b['path']} started and stopped?",
            f"Bank {b['path']} holds {b['type_counts'].get('MusicTrack')} MusicTracks. These events have actions that"
            f" target its music objects: {ids}{' ...' if len(events) > 15 else ''}. For a few of them use get_object on"
            " the event, its actions (Play/Stop/SetState...) and the targets (get_container) to describe which music"
            " container each event starts or stops and which state/switch groups the targets depend on. Record one"
            " finding per bank summarising the pattern (category relationship, subject_type bank, subject_key the bank"
            " id), not one per event.", str(b["bank_id"])))
    switches = db.query(
        "SELECT o.object_id, o.fields_json FROM wwise_object o JOIN asset a ON a.id=o.bank_asset_id"
        " WHERE a.installation_id=? AND o.type_code=12 GROUP BY o.object_id ORDER BY length(o.fields_json) DESC LIMIT 12",
        (inst_id,))
    for sw in switches:
        fields = json.loads(sw["fields_json"] or "{}")
        args = fields.get("arguments") or []
        groups_txt = ", ".join(f"{a.get('group_type')} group {a.get('group_id')}" for a in args) or "none decoded"
        leaves = len(fields.get("decision_tree_leaves") or [])
        tasks.append(InvestigationTask(
            "music_switch", f"What selects the music in switch container {sw['object_id']}?",
            f"Music switch container {sw['object_id']} (fully decoded by the parser) chooses a child from its decision"
            f" tree ({leaves} leaves) using these arguments: {groups_txt}. Use get_object/get_container to see which"
            " playlists/segments each branch leads to, and find_referencing_objects / find_references on the group ids"
            " to find the events or SetState/SetSwitch actions that change them. Record what drives the selection"
            " (category relationship, subject_type object, subject_key the container id).", str(sw["object_id"])))
    unknowns = queries.unknown_structures(db, inst_id, "open")
    music_unknowns = [u for u in unknowns if u["signature"].startswith(MUSIC_UNKNOWN_PREFIXES)]
    rest = [u for u in unknowns if u not in music_unknowns]
    rest.sort(key=lambda u: (u.get("knowledge_uid") is not None, -u["occurrences"]))
    for u in music_unknowns + rest:
        examples = json.dumps((u.get("details") or {}).get("examples", [])[:4], default=str)
        linked = f"\nAn earlier unverified explanation exists: {u['knowledge_uid']} (test it; reject it if wrong)." \
            if u.get("knowledge_uid") else ""
        tasks.append(InvestigationTask(
            "unknown_structure", f"Explain unknown structure {u['signature']}",
            f"Unknown structure signature: {u['signature']}\nCategory: {u['category']}\nOccurrences: {u['occurrences']}\n"
            f"Description: {u['description']}\nExamples: {examples}{linked}\n"
            "Investigate what this is (inspect the bytes/objects/files involved, compare several examples, check"
            " references), then record a hypothesis or finding with evidence (category unknown_structure_explanation,"
            " subject_key = signature). If a deterministic check can prove it, call mark_verified.", u["signature"]))
    return tasks[:limit]


class Investigator:
    def __init__(self, db: Database, paths: AppPaths, inst_id: int, backend: InferenceBackend, model_id: str,
                 progress: Optional[Callable[[AgentProgress], None]] = None, stop: Optional[threading.Event] = None,
                 pause: Optional[threading.Event] = None, max_steps_per_task: int = 14, max_total_steps: int = 400,
                 history_window: int = 8, max_reply_tokens: int = 1500) -> None:
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
        self.max_reply_tokens = max_reply_tokens
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
                f"Steps: {self.state.steps}\nInvalid replies: {self.state.invalid_replies}"
                f" (raw text stored in ai_step with tool '_invalid_reply')\n\n## Tasks\n{summary}\n")
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
            raw = self.backend.chat(messages, json_schema=schema, max_tokens=self.max_reply_tokens, temperature=0.2)
            action = parse_action(raw)
            if action is None:
                failures += 1
                self.state.invalid_replies += 1
                # keep the raw reply so failures can be diagnosed later (not counted as a step)
                info = dict(getattr(self.backend, "last_reply_info", {}) or {})
                self.db.execute(
                    "INSERT INTO ai_step(session_id, step_no, thought, tool, args_json, result_json, created_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (session_id, self.state.steps, f"invalid reply for task: {task.title}", "_invalid_reply",
                     dumps(info), (raw or "")[:4000], now_iso()))
                self.db.commit()
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

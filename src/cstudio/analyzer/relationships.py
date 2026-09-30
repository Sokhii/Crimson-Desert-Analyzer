"""Resolves the Wwise object graph across all banks of an installation.

Object IDs are project-global in Wwise, so a reference in one bank may point
to an object stored in another (or duplicated in several). The graph built
here links::

    Event -> Action -> target object -> ... containers ... -> Sound/MusicTrack -> media (WEM)

and stores, per media source ID, the owning objects, all ancestor containers,
the events that can reach it and the banks involved (``media_context``).
"""

from __future__ import annotations

from collections import defaultdict, deque
from pathlib import PurePosixPath
from typing import Dict, List, Set, Tuple

from cstudio.db.database import Database, dumps

# reference kinds that point from a container to something it plays
DOWN_KINDS = {
    "child", "playlist_item", "playlist_segment", "switch_assoc", "transition_segment", "stinger_segment", "layer_assoc",
}
SOURCE_KINDS = {"source", "track_source"}
MAX_WALK = 50000


class ObjectGraph:
    def __init__(self) -> None:
        self.types: Dict[int, str] = {}
        self.banks_of: Dict[int, Set[int]] = defaultdict(set)
        self.down: Dict[int, Set[int]] = defaultdict(set)
        self.up: Dict[int, Set[int]] = defaultdict(set)
        self.sources_of: Dict[int, Set[int]] = defaultdict(set)  # object -> media ids
        self.owners_of: Dict[int, Set[int]] = defaultdict(set)  # media -> objects
        self.event_actions: Dict[int, Set[int]] = defaultdict(set)
        self.action_targets: Dict[int, Set[int]] = defaultdict(set)

    def descendants(self, start: int) -> Set[int]:
        seen: Set[int] = set()
        queue = deque([start])
        while queue and len(seen) < MAX_WALK:
            node = queue.popleft()
            if node in seen:
                continue
            seen.add(node)
            queue.extend(self.down.get(node, ()))
        return seen

    def ancestors(self, start: int) -> List[int]:
        seen: List[int] = []
        visited: Set[int] = set()
        queue = deque(self.up.get(start, ()))
        while queue and len(visited) < MAX_WALK:
            node = queue.popleft()
            if node in visited:
                continue
            visited.add(node)
            seen.append(node)
            queue.extend(self.up.get(node, ()))
        return seen


def load_graph(db: Database, inst_id: int) -> ObjectGraph:
    g = ObjectGraph()
    bank_assets = "SELECT id FROM asset WHERE installation_id=?"
    for r in db.query(f"SELECT object_id, type_name, bank_asset_id FROM wwise_object WHERE bank_asset_id IN ({bank_assets})", (inst_id,)):
        g.types.setdefault(r["object_id"], r["type_name"])
        g.banks_of[r["object_id"]].add(r["bank_asset_id"])
    for r in db.query(
        f"SELECT from_object_id f, to_id t, kind FROM object_ref WHERE confidence='parsed' AND bank_asset_id IN ({bank_assets})",
        (inst_id,),
    ):
        f, t, kind = r["f"], r["t"], r["kind"]
        if kind in DOWN_KINDS:
            g.down[f].add(t)
            g.up[t].add(f)
        elif kind == "parent":
            g.down[t].add(f)
            g.up[f].add(t)
        elif kind in SOURCE_KINDS:
            g.sources_of[f].add(t)
            g.owners_of[t].add(f)
        elif kind == "event_action":
            g.event_actions[f].add(t)
        elif kind == "action_target":
            g.action_targets[f].add(t)
    return g


def resolve(db: Database, inst_id: int) -> int:
    g = load_graph(db, inst_id)
    _map_named_wems(db, inst_id)
    media_events: Dict[int, Set[int]] = defaultdict(set)
    for event, actions in g.event_actions.items():
        reached: Set[int] = set()
        for action in actions:
            for target in g.action_targets.get(action, ()):
                reached |= g.descendants(target)
        for obj in reached:
            for media in g.sources_of.get(obj, ()):
                media_events[media].add(event)
    all_media: Set[int] = set(g.owners_of)
    for r in db.query(
        "SELECT DISTINCT w.source_id FROM wem w JOIN asset a ON a.id=w.asset_id WHERE a.installation_id=? AND w.source_id>=0",
        (inst_id,),
    ):
        all_media.add(r[0])
    bank_ids = {r["asset_id"]: r["bank_id"] for r in db.query(
        "SELECT b.asset_id, b.bank_id FROM bnk b JOIN asset a ON a.id=b.asset_id WHERE a.installation_id=?", (inst_id,))}
    rows = []
    for media in sorted(all_media):
        owners = sorted(g.owners_of.get(media, ()))
        containers: List[int] = []
        for owner in owners:
            for anc in g.ancestors(owner):
                if anc not in containers:
                    containers.append(anc)
        banks: Set[int] = set()
        for obj in owners + containers:
            for asset_id in g.banks_of.get(obj, ()):
                if asset_id in bank_ids:
                    banks.add(bank_ids[asset_id])
        owner_type = g.types.get(owners[0]) if owners else None
        rows.append((
            inst_id, media, owners[0] if owners else None, owner_type,
            dumps(containers[:200]), dumps([g.types.get(c, "?") for c in containers[:200]]),
            dumps(sorted(media_events.get(media, ()))[:200]), dumps(sorted(banks)),
        ))
    with db.transaction() as conn:
        conn.execute("DELETE FROM media_context WHERE installation_id=?", (inst_id,))
        conn.executemany(
            "INSERT INTO media_context(installation_id, source_id, owner_object_id, owner_type, container_ids_json,"
            " container_types_json, event_ids_json, bank_ids_json) VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def _map_named_wems(db: Database, inst_id: int) -> None:
    """Give non-numeric loose WEM files a source ID when SoundbanksInfo maps their path."""

    unnamed = db.query(
        "SELECT w.id, a.vpath FROM wem w JOIN asset a ON a.id=w.asset_id WHERE a.installation_id=? AND w.source_id<0",
        (inst_id,),
    )
    if not unnamed:
        return
    by_name: Dict[str, int] = {}
    for r in db.query(
        "SELECT media_id, path, cache_path FROM xml_media WHERE asset_id IN (SELECT id FROM asset WHERE installation_id=?)",
        (inst_id,),
    ):
        for p in (r["path"], r["cache_path"]):
            if p:
                by_name[PurePosixPath(p.replace("\\", "/")).name.lower()] = r["media_id"]
    updates = []
    for r in unnamed:
        media = by_name.get(PurePosixPath(r["vpath"]).name.lower())
        if media is not None:
            updates.append((media, r["id"]))
    if updates:
        with db.transaction() as conn:
            conn.executemany("UPDATE wem SET source_id=? WHERE id=?", updates)


def object_neighbourhood(db: Database, inst_id: int, object_id: int, depth: int = 2) -> Dict[str, object]:
    """Small subgraph around an object (used by the UI and the AI tools)."""

    g = load_graph(db, inst_id)
    nodes: Dict[int, str] = {}
    edges: List[Tuple[int, int, str]] = []
    frontier = {object_id}
    for _ in range(depth):
        nxt: Set[int] = set()
        for node in frontier:
            nodes.setdefault(node, g.types.get(node, "?"))
            for child in g.down.get(node, ()):
                edges.append((node, child, "down"))
                nxt.add(child)
            for parent in g.up.get(node, ()):
                edges.append((parent, node, "down"))
                nxt.add(parent)
            for media in g.sources_of.get(node, ()):
                edges.append((node, media, "source"))
                nodes.setdefault(media, "Media")
        frontier = nxt - set(nodes)
    for node in frontier:
        nodes.setdefault(node, g.types.get(node, "?"))
    return {"center": object_id, "nodes": nodes, "edges": sorted(set(edges))[:500]}

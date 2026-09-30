"""Schema-tolerant parser for Wwise ``SoundbanksInfo.xml`` and related XML.

Wwise has changed this file's schema several times (``StreamedFiles`` /
``IncludedMemoryFiles`` in older versions, a ``Media`` table in newer ones), and
the game may ship its own audio XML (for example a ``soundbank.xml``). The
parser therefore walks the element tree generically:

* ``SoundBank`` elements become banks.
* ``Event`` elements become events (attached to the enclosing bank).
* ``File`` / ``Media`` elements with an ``Id`` become media records, with the
  relation to the enclosing bank taken from the ancestor section name.
* Any other element with an ``Id`` and a name becomes a named object
  (state groups, states, switches, game parameters, busses, triggers ...).
* Unrecognised structure is summarised rather than discarded.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional

WRAPPED_ROOT = "CStudioWrappedRoot"  # internal wrapper for documents with several top-level elements
_ID_ATTRS = ("Id", "ID", "id", "ShortId", "ShortID")
_NAME_ATTRS = ("Name", "name", "ShortName")

SECTION_RELATIONS = {
    "includedmemoryfiles": "included_in_memory",
    "referencedstreamedfiles": "referenced_streamed",
    "excludedmemoryfiles": "excluded_from_bank",
    "streamedfiles": "streamed",
    "mediafilesnotinanybank": "not_in_any_bank",
    "prefetchfiles": "prefetch",
    "includedprefetchfiles": "prefetch",
    "media": "media",
}


@dataclass
class XmlBank:
    bank_id: int
    name: str = ""
    path: str = ""
    object_path: str = ""
    language: str = ""
    bank_type: str = ""
    attrs: Dict[str, str] = field(default_factory=dict)


@dataclass
class XmlEvent:
    event_id: int
    name: str = ""
    object_path: str = ""
    bank_id: Optional[int] = None
    attrs: Dict[str, str] = field(default_factory=dict)


@dataclass
class XmlMedia:
    media_id: int
    short_name: str = ""
    path: str = ""
    cache_path: str = ""
    language: str = ""
    streaming: Optional[bool] = None
    location: str = ""
    bank_id: Optional[int] = None
    relation: str = ""
    prefetch_size: Optional[int] = None
    attrs: Dict[str, str] = field(default_factory=dict)


@dataclass
class XmlNamedObject:
    kind: str
    object_id: int
    name: str
    parent_id: Optional[int] = None
    object_path: str = ""
    attrs: Dict[str, str] = field(default_factory=dict)


@dataclass
class SoundbanksInfo:
    root_tag: str = ""
    schema_version: str = ""
    soundbank_version: str = ""
    platform: str = ""
    recognized_schema: bool = False
    multiple_roots: bool = False
    banks: List[XmlBank] = field(default_factory=list)
    events: List[XmlEvent] = field(default_factory=list)
    media: List[XmlMedia] = field(default_factory=list)
    named_objects: List[XmlNamedObject] = field(default_factory=list)
    tag_counts: Dict[str, int] = field(default_factory=dict)
    unrecognized_tags: Dict[str, int] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    def names_by_id(self) -> Dict[int, str]:
        out: Dict[int, str] = {}
        for bank in self.banks:
            if bank.name:
                out.setdefault(bank.bank_id, bank.name)
        for event in self.events:
            if event.name:
                out.setdefault(event.event_id, event.name)
        for obj in self.named_objects:
            if obj.name:
                out.setdefault(obj.object_id, obj.name)
        return out


_KNOWN_TAGS = {
    "soundbanksinfo", "soundbanks", "soundbank", "event", "events", "includedevents", "file", "media",
    "shortname", "path", "cachepath", "objectpath", "name", "rootpaths", "projectroot", "sourcefilesroot",
    "soundbanksroot", "externalsourcesinputfile", "externalsourcesoutputroot", "streamedfiles",
    "includedmemoryfiles", "referencedstreamedfiles", "excludedmemoryfiles", "mediafilesnotinanybank",
    "prefetchfiles", "includedprefetchfiles", "prefetchsize", "gameparameters", "gameparameter",
    "stategroups", "stategroup", "states", "state", "switchgroups", "switchgroup", "switches", "switch",
    "busses", "bus", "auxbusses", "auxbus", "triggers", "trigger", "plugins", "plugin", "audiodevices",
    "audiodevice", "customplatform", "dialogueevents", "dialogueevent", "argumentvalues", "argument",
    "arguments", "externalsources", "externalsource", "acousticTextures".lower(), "acoustictexture",
    "shareset", "sharesets", "custom", "customs", "languagesmap", "language", "platform", "soundbanksinfoversion",
    "alignment", "hash", "usedfiles",
}


def _int_attr(el: ET.Element) -> Optional[int]:
    for key in _ID_ATTRS:
        raw = el.get(key)
        if raw is None:
            continue
        raw = raw.strip()
        try:
            return int(raw, 16) if raw.lower().startswith("0x") else int(raw)
        except ValueError:
            continue
    return None


def _child_text(el: ET.Element, tag: str) -> str:
    for child in el:
        if _local(child.tag).lower() == tag.lower():
            return (child.text or "").strip()
    return ""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _decode(data: bytes) -> bytes:
    if data.startswith(b"\xef\xbb\xbf"):
        return data
    head = data[:200].lower()
    if b"encoding" in head:
        return data
    try:
        data.decode("utf-8")
        return data
    except UnicodeDecodeError:
        text = data.decode("cp949", errors="replace")
        return text.encode("utf-8")


def parse_soundbanksinfo(data: bytes) -> SoundbanksInfo:
    info = SoundbanksInfo()
    try:
        root = ET.fromstring(_decode(data))
    except ET.ParseError as exc:
        # some game XML has no single root element: wrap it
        try:
            text = _decode(data).decode("utf-8", errors="replace")
            text = re.sub(r"^﻿?\s*<\?xml[^>]*\?>", "", text)
            root = ET.fromstring(f"<{WRAPPED_ROOT}>{text}</{WRAPPED_ROOT}>")
            info.errors.append(f"document had no single root element ({exc}); parsed wrapped")
        except ET.ParseError as exc2:
            info.errors.append(f"XML parse error: {exc2}")
            return info
    info.root_tag = _local(root.tag)
    if info.root_tag == WRAPPED_ROOT:
        # several top-level elements: report the real first element, not our wrapper
        first = next(iter(root), None)
        info.root_tag = _local(first.tag) if first is not None else "(empty)"
        info.multiple_roots = True
    info.schema_version = root.get("SchemaVersion", "")
    info.soundbank_version = root.get("SoundbankVersion", "") or root.get("SoundBankVersion", "")
    info.platform = root.get("Platform", "")
    info.recognized_schema = info.root_tag.lower() == "soundbanksinfo"
    media_by_key: Dict[tuple, XmlMedia] = {}
    _walk(root, info, [], None, None, media_by_key)
    info.media = list(media_by_key.values())
    return info


def _walk(el: ET.Element, info: SoundbanksInfo, ancestors: List[str], bank_id: Optional[int],
          parent_named: Optional[int], media_by_key: Dict[tuple, XmlMedia]) -> None:
    tag = _local(el.tag)
    low = tag.lower()
    if tag == WRAPPED_ROOT:
        for child in el:
            _walk(child, info, ancestors, bank_id, parent_named, media_by_key)
        return
    info.tag_counts[tag] = info.tag_counts.get(tag, 0) + 1
    if low not in _KNOWN_TAGS:
        info.unrecognized_tags[tag] = info.unrecognized_tags.get(tag, 0) + 1
    obj_id = _int_attr(el)
    attrs = {k: v for k, v in el.attrib.items()}
    next_bank = bank_id
    next_parent = parent_named
    if low == "soundbank" and obj_id is not None:
        bank = XmlBank(
            bank_id=obj_id,
            name=_child_text(el, "ShortName") or el.get("Name", "") or el.get("ShortName", ""),
            path=_child_text(el, "Path") or el.get("Path", ""),
            object_path=_child_text(el, "ObjectPath") or el.get("ObjectPath", ""),
            language=el.get("Language", ""),
            bank_type=el.get("Type", ""),
            attrs=attrs,
        )
        info.banks.append(bank)
        next_bank = obj_id
    elif low in ("event", "dialogueevent") and obj_id is not None:
        info.events.append(
            XmlEvent(
                event_id=obj_id,
                name=el.get("Name", "") or _child_text(el, "Name") or _child_text(el, "ShortName"),
                object_path=el.get("ObjectPath", "") or _child_text(el, "ObjectPath"),
                bank_id=bank_id,
                attrs=attrs,
            )
        )
    elif low in ("file", "media") and obj_id is not None:
        relation = ""
        for anc in reversed(ancestors):
            rel = SECTION_RELATIONS.get(anc.lower())
            if rel:
                relation = rel
                break
        streaming_raw = el.get("Streaming")
        prefetch = _child_text(el, "PrefetchSize")
        record = XmlMedia(
            media_id=obj_id,
            short_name=_child_text(el, "ShortName") or el.get("ShortName", ""),
            path=_child_text(el, "Path") or el.get("Path", ""),
            cache_path=_child_text(el, "CachePath") or el.get("CachePath", ""),
            language=el.get("Language", ""),
            streaming=None if streaming_raw is None else streaming_raw.lower() == "true",
            location=el.get("Location", ""),
            bank_id=bank_id,
            relation=relation,
            prefetch_size=int(prefetch) if prefetch.isdigit() else None,
            attrs=attrs,
        )
        key = (obj_id, bank_id, relation)
        existing = media_by_key.get(key)
        if existing is None:
            media_by_key[key] = record
        else:
            for attr in ("short_name", "path", "cache_path", "language", "location"):
                if not getattr(existing, attr) and getattr(record, attr):
                    setattr(existing, attr, getattr(record, attr))
    elif obj_id is not None:
        name = ""
        for key in _NAME_ATTRS:
            if el.get(key):
                name = el.get(key, "")
                break
        if not name:
            name = _child_text(el, "Name") or _child_text(el, "ShortName")
        if name:
            info.named_objects.append(
                XmlNamedObject(
                    kind=tag,
                    object_id=obj_id,
                    name=name,
                    parent_id=parent_named,
                    object_path=el.get("ObjectPath", "") or _child_text(el, "ObjectPath"),
                    attrs=attrs,
                )
            )
            next_parent = obj_id
    for child in el:
        _walk(child, info, ancestors + [tag], next_bank, next_parent, media_by_key)

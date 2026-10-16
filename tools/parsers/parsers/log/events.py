# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Recognition of events in OCUDU logs, such as random access, UE creation, RRC messages, handovers and radio link
failures.

Events are recognized by their message, since most failures are logged at info level. Warning and error lines that no
pattern recognizes are reported as generic "warning" or "error" events, in the category of the same name.

Some messages change format with the log level of their logger. At debug level, the scheduler prints its slot events
one per continuation line below the entry header, while at info level it prints them inline. Patterns are therefore
bound to the level of the entry, and a log entry may hold several events.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import preamble
from .preamble import ENTRY_START_PATTERN

# Event categories, in display order.
CATEGORIES = ("ra", "lifecycle", "rrc", "mobility", "failure", "warning", "error")
# Category of the records that are not events but bind a DU UE index to its RNTI, for UeTracker.
BINDING = "binding"


@dataclass(frozen=True)
class EventPattern:
    """A kind of event: its type, category, logger and the message pattern that recognizes it.

    With item, the entry holds one event per match of item, searched in the message or, with in_body, in each
    continuation line. With level, the pattern only applies to entries of that level.
    """

    type: str
    category: str
    layer: str
    pattern: re.Pattern
    item: re.Pattern | None = None
    level: str | None = None
    in_body: bool = False


def _p(type_: str, category: str, layer: str, pattern: str, item: str | None = None, **kwargs) -> EventPattern:
    return EventPattern(type_, category, layer, re.compile(pattern), re.compile(item) if item else None, **kwargs)


_PRACH_INFO = r"prach\((?:ra|msgb)-rnti=0x\w+ preamble=(?P<preamble>\d+)(?: ssb=\d+)? tc-rnti=(?P<rnti>0x\w+)\)"
# The pci is of older builds, the ssb of newer ones.
_PRACH_DEBUG = (r"^- PRACH: slot=[^\s,]+,? (?:pci=\d+ )?preamble=(?P<preamble>\d+) (?:ssb=\d+ )?(?:ra|msgb)-rnti=0x\w+ "
                r"temp_crnti=(?P<rnti>0x\w+)")
# RRC messages as the RRC logs them at info, e.g. "DCCH UL rrcSetupComplete", or at debug, with the direction, SRB and
# size, e.g. "Rx SRB1 DCCH UL rrcSetupComplete (96 B)", or of older builds without the SRB.
_RRC_PREFIX = r"ue=(?P<ue>\d+) c-rnti=(?P<rnti>0x\w+): (?:[RT]x (?:SRB\d+ )?)?"
_RRC_SUFFIX = r"(?: \(\d+ B\))?$"

EVENT_PATTERNS = (
    _p("prach", "ra", "SCHED", r"^Processed slot events pci=\d+: ", _PRACH_INFO, level="I"),
    # Older builds print no pci in the header.
    _p("prach", "ra", "SCHED", r"^Processed slot events(?: pci=\d+)?:$", _PRACH_DEBUG, level="D", in_body=True),
    _p("msg3", "ra", "MAC", r"UL rnti=(?P<rnti>0x\w+) subPDUs: \[CCCH"),
    _p("conres", "ra", "MAC", r"DL PDU: ue=(?P<ue>\d+) rnti=(?P<rnti>0x\w+) size=\d+: CON_RES"),
    _p("ue_create", "lifecycle", "DU-MNG", r'ue=(?P<ue>\d+)(?: rnti=(?P<rnti>0x\w+))? proc="UE Create": Procedure started'),
    # A UE created without its RNTI, e.g. the target of a handover, gets it at its first configuration.
    _p("ue_config", BINDING, "DU-MNG", r'ue=(?P<ue>\d+) rnti=(?P<rnti>0x\w+) proc="UE Configuration": Procedure started'),
    _p("ue_delete", "lifecycle", "DU-MNG", r'ue=(?P<ue>\d+) proc="UE Delete": Procedure finished successfully'),
    _p("rrc_setup_complete", "rrc", "RRC", _RRC_PREFIX + r"DCCH UL rrcSetupComplete" + _RRC_SUFFIX),
    _p("rrc_release", "rrc", "RRC", _RRC_PREFIX + r"DCCH DL rrcRelease" + _RRC_SUFFIX),
    _p("rrc_reest_request", "rrc", "RRC", _RRC_PREFIX + r"CCCH UL rrcReestablishmentRequest" + _RRC_SUFFIX),
    _p("ho_trigger", "mobility", "CU-CP", r"ue=(?P<ue>\d+): Trigger intra-CU \((?:inter|intra)-DU\) handover"),
    _p("ho_preparation", "mobility", "NGAP", r"ue=(?P<ue>\d+) ran_ue=\d+ amf_ue=\d+: Starting HO preparation"),
    # A UE index or C-RNTI clash with a live UE, among others, which leaves no context.
    _p("ue_create_failed", "failure", "DU-MNG",
       r'ue=(?P<ue>\d+)(?: rnti=(?P<rnti>0x\w+))? proc="UE Create": Procedure failed\. Cause: (?P<cause>.*?)\.?$'),
    _p("rlf", "failure", "DU-MNG", r'ue=(?P<ue>\d+) rnti=(?P<rnti>0x\w+): RLF detected with cause "(?P<cause>[^"]+)"'),
    _p("rrc_reest_failed", "failure", "RRC",
       r'ue=(?P<ue>\d+) c-rnti=(?P<rnti>0x\w+): "RRC Reestablishment Procedure".* failed\.(?: Cause: (?P<cause>.*))?'),
    _p("rrc_reest_rejected", "failure", "RRC", r"ue=(?P<ue>\d+) c-rnti=(?P<rnti>0x\w+): Rejecting RRC Reestablishment.*Cause: (?P<cause>.*?)\.(?: |$)"),
    # The ConRes CE sent but never ACKed, before the timer expiring before the ConRes CE was scheduled, whose format
    # differs across builds.
    _p("conres_unacked", "failure", "SCHED",
       r"ue=(?P<ue>\d+) rnti=(?P<rnti>0x\w+): ra-ContentionResolutionTimer \(.*\) expired, but"),
    _p("conres_timeout", "failure", "SCHED", r"ue=(?P<ue>\d+) rnti=(?P<rnti>0x\w+): ra-ContentionResolutionTimer.* expired"),
    _p("rrc_setup_timeout", "failure", "RRC", r'ue=(?P<ue>\d+) c-rnti=(?P<rnti>0x\w+): "RRC Setup Procedure" timed out'),
)

# Substrings of the entry headers that can hold events, to skip the other lines without parsing them. Usable as a
# bytes pattern too, to skip lines before decoding them.
CANDIDATE_PATTERN = (
    r'Processed slot events|subPDUs: \[CCCH|CON_RES|proc="UE (?:Create|Delete|Configuration)"|rrcSetupComplete|rrcRelease'
    r"|rrcReestablishmentRequest|Trigger intra-CU|Starting HO preparation|RLF detected|Reestablishment"
    r'|ra-ContentionResolutionTimer|"RRC Setup Procedure" timed out|\] \[[WE]\] '
)

_CANDIDATE_RE = re.compile(CANDIDATE_PATTERN)
_ENTRY_START_RE = re.compile(ENTRY_START_PATTERN)
_LEVEL_EVENTS = {"W": "warning", "E": "error"}
# UE identifiers of the warning and error lines that no pattern recognizes, in one pass over the message.
# Not the RA-RNTI or MsgB-RNTI of a random access, which is no UE's.
_GENERIC_IDS_RE = re.compile(r"\bue=(?P<ue>\d+)\b|(?<![\w-])(?:c-|tc-)?rnti=(?P<rnti>0x[0-9a-fA-F]+)\b")
_BODY_KEYS = {(p.layer, p.level) for p in EVENT_PATTERNS if p.in_body}
# Patterns of each logger, in EVENT_PATTERNS order.
_PATTERNS_BY_LAYER: dict[str, list[EventPattern]] = {}
for _pattern in EVENT_PATTERNS:
    _PATTERNS_BY_LAYER.setdefault(_pattern.layer, []).append(_pattern)


def is_candidate(line: str) -> bool:
    """Returns whether an entry header may hold events. Cheap check to skip most lines before parse()."""
    return _CANDIDATE_RE.search(line) is not None


def is_entry_start(line: str) -> bool:
    """Returns whether a line begins a log entry, rather than continuing the previous one."""
    return _ENTRY_START_RE.match(line) is not None


def has_body(preamble_match: re.Match) -> bool:
    """Returns whether the events of an entry may be in its continuation lines, which parse() then needs."""
    return (preamble_match.group("layer").strip(), preamble_match.group("level")) in _BODY_KEYS


def parse(line: str, body: Sequence[str] = (), preamble_match: re.Match | None = None) -> list[dict[str, Any]]:
    """Parses a log entry, given by its header line and continuation lines, into its event records.

    Each record has the timestamp, type, category, layer, level, ue and rnti (None when not logged), cause (or None)
    and the message text. Records of category BINDING are not events, see UeTracker.
    """
    if preamble_match is None:
        preamble_match = preamble.match_preamble(line)
    if not preamble_match or (timestamp := preamble.to_datetime(preamble_match.group("timestamp"))) is None:
        return []
    layer = preamble_match.group("layer").strip()
    level = preamble_match.group("level")
    text = line[preamble_match.end():].strip()

    found: list[tuple[str, str, dict[str, Any], str]] = []
    for ev in _PATTERNS_BY_LAYER.get(layer, ()):
        if ev.level is not None and ev.level != level:
            continue
        m = ev.pattern.search(text)
        if not m:
            continue
        if ev.item is None:
            found.append((ev.type, ev.category, m.groupdict(), text))
        elif ev.in_body:
            for body_line in map(str.strip, body):
                if im := ev.item.search(body_line):
                    found.append((ev.type, ev.category, {**m.groupdict(), **im.groupdict()}, body_line.removeprefix("- ")))
        else:
            for im in ev.item.finditer(text):
                found.append((ev.type, ev.category, {**m.groupdict(), **im.groupdict()}, im.group(0)))
        break
    if not found and (level_type := _LEVEL_EVENTS.get(level or "")):
        groups: dict[str, str | None] = {"ue": None, "rnti": None}
        for m in _GENERIC_IDS_RE.finditer(text):
            name = m.lastgroup
            if groups[name] is None:
                groups[name] = m.group(name)
                if groups["ue"] is not None and groups["rnti"] is not None:
                    break
        found.append((level_type, level_type, groups, text))

    return [
        {
            "timestamp": timestamp,
            "type": type_,
            "category": category,
            "layer": layer,
            "level": level,
            "ue": int(groups["ue"]) if groups.get("ue") is not None else None,
            "rnti": groups.get("rnti"),
            "cause": groups.get("cause"),
            "text": ev_text,
        }
        for type_, category, groups, ev_text in found
    ]


def iter_events(lines: Iterable[str]) -> Iterator[tuple[int, dict[str, Any]]]:
    """Yields the events of a log, with the line number of the entry header they come from, starting at 1, and the
    BINDING records, see parse().
    """
    pending: tuple[int, str, re.Match, list[str]] | None = None
    for line_no, line in enumerate(lines, start=1):
        if pending is not None:
            if not is_entry_start(line):
                pending[3].append(line)
                continue
            for ev in parse(pending[1], pending[3], pending[2]):
                yield pending[0], ev
            pending = None
        if not is_candidate(line) or not (m := preamble.match_preamble(line)):
            continue
        if has_body(m):
            pending = (line_no, line, m, [])
        else:
            for ev in parse(line, preamble_match=m):
                yield line_no, ev
    if pending is not None:
        for ev in parse(pending[1], pending[3], pending[2]):
            yield pending[0], ev


# Loggers of the DU, whose "ue" is the DU UE index, and of the CU-UP, whose "ue" is the CU-UP UE index, which no other
# event names. The others log the CU-CP UE index; PDCP logs it for SRBs, and the CU-UP UE index for DRBs.
_DU_LOGGERS = frozenset({"MAC", "SCHED", "DU-MNG", "DU-F1", "DU-F1-U", "RLC", "PHY"})
_CU_UP_LOGGERS = frozenset({"CU-UP", "CU-UP-E1", "CU-F1-U", "SDAP", "GTPU"})


@dataclass
class UeLane:
    """One UE context of the DU, from its random access or creation to its deletion or failed creation."""

    id: int
    du_ue: int | None
    rnti: str | None
    t_start: datetime | float
    t_end: datetime | float | None = None
    created: bool = False
    deleted: bool = False
    # Whether its creation failed, which closes it.
    failed: bool = False
    # CU-CP UE index, from the CU events of its RNTI, which differs from the DU one.
    cu_ue: int | None = None


class UeTracker:
    """Assigns events to the UE contexts they belong to, given the events in log order.

    A UE context is identified by its RNTI, from the random access to the UE deletion, and by its DU UE index once
    created. Events of the CU-CP are matched through the RNTI of the RRC messages of the same CU-CP UE index. BINDING
    records give the RNTI of a context created without it, which its later events, e.g. a contention-free random
    access, are then matched by.
    """

    def __init__(self):
        self.lanes: list[UeLane] = []
        self._by_rnti: dict[str, UeLane] = {}
        self._by_du_ue: dict[int, UeLane] = {}
        self._cu_ue_rnti: dict[int, str] = {}
        # Per DU UE index of an ongoing creation, its lane and the lanes that it displaced from the DU UE index and RNTI.
        self._creating: dict[int, tuple[UeLane, UeLane | None, UeLane | None]] = {}

    def assign(self, event: dict[str, Any]) -> int | None:
        """Returns the id of the UE context of an event, or None for events of no UE."""
        return self.assign_ids(event["timestamp"], event["type"], event.get("category"), event["layer"], event["ue"], event["rnti"])

    def assign_ids(self, t: Any, type_: str, category: str | None, layer: str, ue: int | None, rnti: str | None) -> int | None:
        """Like assign(), given the fields of the event. Timestamps only need to compare, e.g. epoch seconds."""
        lane = self._lane(t, type_, category, layer, ue, rnti)
        if lane is not None and t > (lane.t_end or lane.t_start):
            lane.t_end = t
        return lane.id if lane is not None else None

    def _lane(self, t: Any, type_: str, category: str | None, layer: str, ue: int | None, rnti: str | None) -> UeLane | None:
        du = layer in _DU_LOGGERS
        if layer in _CU_UP_LOGGERS:
            ue = None
        if type_ == "ue_create":
            prev_du, prev_rnti = self._by_du_ue.get(ue), self._by_rnti.get(rnti) if rnti else None
            lane = prev_rnti
            if lane is None or lane.du_ue is not None:
                lane = self._new_lane(t, rnti)
            lane.du_ue, lane.created = ue, True
            self._by_du_ue[ue] = lane
            self._creating[ue] = (lane, prev_du, prev_rnti)
            return lane
        if type_ == "ue_create_failed":
            return self._fail_creation(ue, rnti)
        if category == BINDING:
            # Only within the lifetime of the context, since DU UE indexes are soon reused.
            lane = self._by_du_ue.get(ue) if ue is not None else None
            if lane is not None and lane.rnti is None and rnti:
                lane.rnti = rnti
                self._by_rnti[rnti] = lane
            return lane
        if type_ == "ue_delete":
            self._creating.pop(ue, None)
            lane = self._by_du_ue.pop(ue, None)
            if lane is not None:
                lane.deleted = True
                if lane.rnti and self._by_rnti.get(lane.rnti) is lane:
                    del self._by_rnti[lane.rnti]
            return lane
        if rnti:
            if not du and ue is not None:
                self._cu_ue_rnti[ue] = rnti
            lane = self._by_du_ue.get(ue) if du and ue is not None else None
            # A DU UE index of another RNTI is of a context whose deletion was not logged, and reused.
            if lane is not None and lane.rnti is not None and lane.rnti != rnti:
                lane.du_ue = None
                del self._by_du_ue[ue]
                lane = None
            if lane is None:
                lane = self._by_rnti.get(rnti) or self._new_lane(t, rnti)
            if lane.rnti is None:
                lane.rnti = rnti
                self._by_rnti[rnti] = lane
            if du and ue is not None and lane.du_ue is None:
                lane.du_ue = ue
                self._by_du_ue[ue] = lane
            if not du and ue is not None and lane.cu_ue is None:
                lane.cu_ue = ue
            return lane
        if ue is None:
            return None
        if du:
            return self._by_du_ue.get(ue)
        cu_rnti = self._cu_ue_rnti.get(ue)
        return self._by_rnti.get(cu_rnti) if cu_rnti else None

    def _fail_creation(self, ue: int | None, rnti: str | None) -> UeLane | None:
        """Closes the lane of a failed creation and gives its DU UE index and RNTI back to the UEs that held them."""
        lane, prev_du, prev_rnti = self._creating.pop(ue, (None, None, None)) if ue is not None else (None, None, None)
        if lane is None or lane.rnti != rnti:
            return None
        lane.created, lane.failed = False, True
        if self._by_du_ue.get(ue) is lane:
            del self._by_du_ue[ue]
            # The clash is often with a live UE of the same index, which the creation must not end.
            if prev_du is not None and prev_du is not lane and not prev_du.deleted:
                self._by_du_ue[ue] = prev_du
        if rnti and self._by_rnti.get(rnti) is lane:
            del self._by_rnti[rnti]
            if prev_rnti is not None and prev_rnti is not lane and not prev_rnti.deleted:
                self._by_rnti[rnti] = prev_rnti
        return lane

    def _new_lane(self, t: datetime | float, rnti: str | None) -> UeLane:
        lane = UeLane(len(self.lanes), None, rnti, t)
        self.lanes.append(lane)
        if rnti:
            self._by_rnti[rnti] = lane
        return lane

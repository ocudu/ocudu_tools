# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Trace of a run: the UE contexts of its F1AP pcap, joined with the UE events of its logs, and the traces of its UEs
with its NGAP and E1AP pcaps.
"""

from __future__ import annotations

from typing import Any

from parsers.correlate import ues as ues_util

from .filters import filter_rows
from .store import Store

# Lanes and events read from a store to join its lanes, without limit.
_ALL = 10**9
# Identifiers of the UE of a lane shown on the lanes of the other sources of its UE, e.g. on an E1AP lane: the NGAP
# ones, which the AMF keeps for the UE across handovers. The UE trace number is not shown.
UE_IDS = ("ran_ngap", "amf_ngap")
# Fields of the events that trace filters compare, with the UE index and RNTI of its lane for events without them.
FILTER_FIELDS = ("type", "category", "layer", "level", "ue", "rnti", "cause", "text")


def lane_ids(lane: dict[str, Any]) -> dict[str, list[str]]:
    """The identifiers of a lane: the protocol ones of its label, e.g. "du_f1ap", the DU UE index ("du_ue"), the
    CU-CP one ("cu_ue") and the RNTI, and its "ids", e.g. those of a run trace.
    """
    out: dict[str, list[str]] = {}
    for token in (lane.get("label") or "").split():
        if "=" in token:
            name, value = token.split("=", 1)
            out[name] = [value]
    for name, field in (("du_ue", "ue"), ("cu_ue", "cu_ue"), ("rnti", "rnti")):
        if lane.get(field) is not None and lane[field] != "":
            out[name] = [str(lane[field])]
    for name, values in (lane.get("ids") or {}).items():
        known = out.setdefault(name, [])
        known.extend(v for v in values if v not in known)
    return out


def lane_keys(lane: dict[str, Any]) -> dict[str, str]:
    """The first value of each identifier of a lane, see lane_ids(), which rows can be grouped by."""
    return {name: values[0] for name, values in lane_ids(lane).items() if values}


def group_trace(trace: dict[str, Any], by: str, max_lanes: int, limit: int) -> dict[str, Any]:
    """A trace, as Store.trace() returns it with all its lanes and events, with the lanes of the same identifier by,
    see lane_keys(), merged into one, labelled with it, then reduced to max_lanes lanes and limit events. Lanes
    without the identifier keep their own. Each lane gets "ids", the values of each identifier of the lanes it merges,
    and "contexts", the time span and ids of each of them. Each event gets "context", the index of its lane there.
    """
    merged: dict[Any, dict[str, Any]] = {}
    # Merged lane and index in its contexts of each lane.
    lane_of: dict[Any, tuple[Any, int]] = {}
    for lane in trace["lanes"]:
        keys = lane_keys(lane)
        # A failed creation has the DU UE index or RNTI of another UE, so it keeps its own row.
        value = keys.get(by) if not lane.get("failed") else None
        key = f"{by}={value}" if value is not None else lane["lane"]
        if key not in merged:
            merged[key] = {**lane, "lane": key, "ids": {}, "contexts": []}
            if value is not None:
                merged[key].update(label=key, ue=None, rnti=None, cu_ue=None)
        m = merged[key]
        ids = lane_ids(lane)
        lane_of[lane["lane"]] = (key, len(m["contexts"]))
        m["contexts"].append({"t_start": lane["t_start"], "t_end": lane["t_end"], "open": lane["open"], "ids": ids})
        for name, lane_values in ids.items():
            values = m["ids"].setdefault(name, [])
            values.extend(v for v in lane_values if v not in values)
        m["t_start"] = min(m["t_start"], lane["t_start"])
        m["t_end"] = max(m["t_end"], lane["t_end"])
        m["open"] = m["open"] or lane["open"]
    lanes = sorted(merged.values(), key=lambda lane: lane["t_start"])
    shown = {lane["lane"] for lane in lanes[:max_lanes]}
    events = []
    for ev in trace["events"]:
        key, context = lane_of.get(ev["lane"], (None, None)) if ev["lane"] is not None else (None, None)
        if key is None or key in shown:
            events.append({**ev, "lane": key, "context": context})
    return {
        "lanes": lanes[:max_lanes],
        "total_lanes": len(lanes),
        "events": events[:limit],
        "total_events": len(events),
        "truncated": len(events) > limit,
    }


def filter_trace(trace: dict[str, Any], expr: str, max_lanes: int, limit: int) -> dict[str, Any]:
    """A trace, as Store.trace() returns it with all its lanes and events, reduced to the events matching a filter
    expression over FILTER_FIELDS and the lanes they are in, then to max_lanes lanes and limit events.
    """
    lanes = {lane["lane"]: lane for lane in trace["lanes"]}
    rows = []
    for ev in trace["events"]:
        lane = lanes.get(ev["lane"], {})
        rows.append({**ev, "ue": ev["ue"] if ev["ue"] is not None else lane.get("ue"), "rnti": ev["rnti"] or lane.get("rnti")})
    kept = filter_rows(rows, expr, FILTER_FIELDS)
    in_lanes = {ev["lane"] for ev in kept}
    shown = [lane for lane in trace["lanes"] if lane["lane"] in in_lanes]
    shown_ids = {lane["lane"] for lane in shown[:max_lanes]}
    events = [ev for ev in kept if ev["lane"] is None or ev["lane"] in shown_ids]
    return {
        "lanes": shown[:max_lanes],
        "total_lanes": len(shown),
        "events": events[:limit],
        "total_events": len(events),
        "truncated": len(events) > limit,
    }


class RunTrace:
    """Joins the lanes of the logs of a run to the UE contexts of its F1AP pcap, see join_lanes(), and follows its UEs
    through them and its NGAP and E1AP contexts, see parsers.correlate.ues.ue_traces().
    """

    def __init__(self, anchor: tuple[int, Store], logs: list[tuple[int, Store]], cores: list[tuple[int, Store]] = ()):
        """anchor is the F1AP pcap of the run, logs its logs and cores its NGAP and E1AP pcaps, each as (source id,
        store).
        """
        self._sources = [anchor, *logs]
        lanes = {source: _lanes(store) for source, store in self._sources}
        self._labels = {(source, lane["lane"]): lane["label"] for source, ls in lanes.items() for lane in ls}
        joined, failed = _join(self._sources, lanes)
        self._lanes = sorted(joined + failed, key=lambda ue: (ue.t_start, ue.key))
        self._failed = {ue.key for ue in failed}
        core_contexts = [ctx for source, store in cores for ctx in _contexts(source, _lanes(store), store.lane_links())]
        traces = ues_util.ue_traces(joined, core_contexts)
        self._cores = dict(cores)
        # Identifiers of the UE of each NGAP and E1AP lane, as (source, lane), see UE_IDS.
        self._core_ids = {part: {k: tr.ids[k] for k in UE_IDS if k in tr.ids} for tr in traces for part in tr.cores}
        self._lane_of = {part: lane.key for lane in self._lanes for part in lane.parts}

    def trace(
        self,
        t0: float | None,
        t1: float | None,
        max_lanes: int,
        limit: int,
        sources: set[int] | None = None,
        filter_expr: str | None = None,
        group_by: str | None = None,
    ) -> dict[str, Any]:
        """Like Store.trace(), over the joined lanes, with the source of each event. Lane ids are strings.

        With sources, only their events and the lanes they have events in, e.g. the events of a log on the UE contexts
        of the F1AP pcap. With the F1AP pcap, the RRC events of the logs are left out, since its packets carry them.
        With an NGAP or E1AP pcap as the only source, its own lanes, with the UE_IDS of the UE of each. With filter_expr, see
        filter_trace(), and with group_by, see group_trace().
        """
        if sources and len(sources) == 1 and (core := next(iter(sources))) in self._cores:
            return self._core_trace_of(core, t0, t1, max_lanes, limit, filter_expr, group_by)
        if group_by:
            return group_trace(self.trace(t0, t1, _ALL, _ALL, sources, filter_expr), group_by, max_lanes, limit)
        if filter_expr:
            return filter_trace(self.trace(t0, t1, _ALL, _ALL, sources), filter_expr, max_lanes, limit)
        lo = -float("inf") if t0 is None else t0
        hi = float("inf") if t1 is None else t1
        active = [
            lane for lane in self._lanes
            if lane.t_start <= hi and (lane.t_end >= lo or lane.open)
            and (sources is None or any(source in sources for source, _ in lane.parts))
        ]
        shown = {lane.key for lane in active[:max_lanes]}
        events = []
        anchor = self._sources[0][0]
        # The packets of the F1AP pcap carry the RRC messages that its logs print, so they stand for them.
        drop_log_rrc = sources is None or anchor in sources
        for source, store in self._sources:
            if sources is not None and source not in sources:
                continue
            for ev in store.trace(t0, t1, _ALL, _ALL)["events"]:
                if drop_log_rrc and source != anchor and ev["category"] == "rrc":
                    continue
                key = self._lane_of.get((source, ev["lane"])) if ev["lane"] is not None else None
                if key is not None and key not in shown:
                    continue
                events.append({**ev, "lane": key, "source": source})
        events.sort(key=lambda e: e["t"])
        lanes = [
            {"lane": lane.key, "t_start": lane.t_start, "t_end": lane.t_end, "ue": _int(lane.first("du_ue")),
             "rnti": lane.first("rnti"), "label": self._labels.get(lane.parts[0]), "cu_ue": _int(lane.first("cu_ue")),
             "open": lane.open, "ids": {k: v for k, v in lane.ids.items() if k != "ue_trace"}, "failed": lane.key in self._failed}
            for lane in active[:max_lanes]
        ]
        return {
            "lanes": lanes,
            "total_lanes": len(active),
            "events": events[:limit],
            "total_events": len(events),
            "truncated": len(events) > limit,
        }


    def _core_trace_of(self, source: int, t0: float | None, t1: float | None, max_lanes: int, limit: int,
                       filter_expr: str | None, group_by: str | None) -> dict[str, Any]:
        trace = self._cores[source].trace(t0, t1, _ALL, _ALL)
        for lane in trace["lanes"]:
            if ids := self._core_ids.get((source, lane["lane"])):
                lane["ids"] = ids
        trace["events"] = [{**ev, "source": source} for ev in trace["events"]]
        if filter_expr:
            trace = filter_trace(trace, filter_expr, _ALL, _ALL)
        if group_by:
            return group_trace(trace, group_by, max_lanes, limit)
        return _limited(trace, max_lanes, limit)

def _limited(trace: dict[str, Any], max_lanes: int, limit: int) -> dict[str, Any]:
    """A trace with all its lanes and events reduced to max_lanes lanes, in start order, and limit events."""
    lanes = trace["lanes"][:max_lanes]
    shown = {lane["lane"] for lane in lanes}
    events = [ev for ev in trace["events"] if ev["lane"] is None or ev["lane"] in shown]
    return {
        "lanes": lanes,
        "total_lanes": len(trace["lanes"]),
        "events": events[:limit],
        "total_events": len(events),
        "truncated": len(events) > limit,
    }


def join_lanes(anchor: tuple[int, Store], logs: list[tuple[int, Store]]) -> list[ues_util.Ue]:
    """The UEs of a run from the UE contexts of its F1AP pcap, then of its logs, see parsers.correlate.ues.combine(),
    and the failed UE creations of its logs, UEs of their own. UE keys are "<source id>:<lane id>".
    """
    sources = [anchor, *logs]
    joined, failed = _join(sources, {source: _lanes(store) for source, store in sources})
    return sorted(joined + failed, key=lambda ue: (ue.t_start, ue.key))


def _join(sources: list[tuple[int, Store]], lanes: dict[int, list[dict[str, Any]]]) -> tuple[list[ues_util.Ue], list[ues_util.Ue]]:
    """The UEs that the lanes of each source join, see join_lanes(), and the failed UE creations, which join none, since
    their DU UE index or RNTI is of another UE.
    """
    stores = dict(sources)
    joined = ues_util.combine(*(
        _contexts(source, [lane for lane in ls if not lane["failed"]], stores[source].lane_links())
        for source, ls in lanes.items()
    ))
    failed = [
        ues_util.Ue(f"{source}:{lane['lane']}", lane["t_start"], lane["t_end"], False,
                    {k: [v] for k, v in lane_keys(lane).items()}, [(source, lane["lane"])], lane["t_start"])
        for source, ls in lanes.items() for lane in ls if lane["failed"]
    ]
    return joined, failed


def _lanes(store: Store) -> list[dict[str, Any]]:
    return store.trace(None, None, _ALL, 0)["lanes"]


def _contexts(source: int, lanes: list[dict[str, Any]], links: dict[int, dict[str, Any]]) -> list[ues_util.Context]:
    return [
        ues_util.Context(source, lane["lane"], lane["t_start"], lane["t_end"], lane_keys(lane), lane["open"], links.get(lane["lane"], {}))
        for lane in lanes
    ]


def _int(value: str | None) -> int | None:
    return None if value is None else int(value)

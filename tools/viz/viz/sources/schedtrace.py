# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Source type of the scheduler event traces of OCUDU (schedtrace_cell<N>.bin): the resource grid of their cell."""

from __future__ import annotations

import dataclasses
import json
import mmap
import re
import struct
import warnings
from collections import Counter
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from parsers import schedtrace
from parsers.schedtrace import grid as grid_util
from parsers.schedtrace import harq as harq_util
from parsers.schedtrace import ue_metrics

from .. import parallel
from .base import DatasetWriter, ProgressFn, RunIdentity, column_type
from .log_metrics import _parsers_version

_NAME_RE = re.compile(r"schedtrace_cell\d+\.bin")
# CRBs of a row of the occupancy grid.
GROUP_SIZE = 4
# Slots of the bins of the stored occupancy, from the finest, each a multiple of the one before.
LEVELS = (20, 100, 400, 2000)
# A window of at most this many slots shows its allocations, and of at most SLOT_HEAT_MAX slots its occupancy per slot,
# both decoded from the trace; larger windows show the stored occupancy.
RECTS_MAX = 400
SLOT_HEAT_MAX = 4000
# Slots before and after an allocation in which its HARQ transaction is looked for: one SFN cycle.
HARQ_WINDOW = 1024
# Time around a HARQ transaction whose slots are placed to time its rows, in seconds.
HARQ_MARGIN_S = 0.05
# Chunks per worker of a trace parsed in the pool, so that results arrive while the others are parsed, and the fewest
# events per chunk.
_CHUNKS_PER_WORKER = 4
_MIN_CHUNK_EVENTS = 2000
# Share of the slots with allocations that have both DL and UL shared or control channels from which the cell is
# taken as FDD: TDD only mixes them in special slots, without PDSCH and PUSCH together.
_FDD_MIN_SHARE = 0.05
_U32 = struct.Struct("<I")
# Frames after which the SFN wraps.
_NOF_SFNS = 1024
# Datasets of the aggregates per bin of ue_metrics.BIN_S, and of the rows of each kind of ue_metrics.slot_items(), with
# their label.
_UE_DATASET, _CELL_DATASET = "ue_metrics", "cell_metrics"
_DATASET_LABELS = {_UE_DATASET: "UE metrics per 10 ms", _CELL_DATASET: "cell metrics per 10 ms", "dl_grants": "DL grants",
                   "ul_grants": "UL grants", "harq_acks": "HARQ-ACK reports", "csi_reports": "CSI reports"}
# Fields of the datasets that are not numbers.
_NON_NUMBER_FIELDS = {"rnti", "status"}


class SchedtraceSource:
    """The resource grid of the cell of a scheduler event trace. Records are its events, the slots among them in the
    grid, with a summary as text and their decoding as detail.
    """

    name = "schedtrace"
    # The schema hash invalidates the parses of the trace when the schemas change.
    version = f"9+parsers-{_parsers_version()}+schema-{schedtrace.schema_hash()[:12]}"

    def accepts(self, path: Path) -> bool:
        if not _NAME_RE.fullmatch(path.name):
            return False
        try:
            with path.open("rb") as f:
                head = f.read(4)
            # Traces without the header of the file format are accepted too, so that parsing tells why they fail.
            return head == schedtrace.MAGIC or (len(head) == 4 and _U32.unpack(head)[0] <= path.stat().st_size)
        except OSError:
            return False

    def on_request(self, path: Path) -> bool:
        """Whether a trace is parsed only on request with the other files of its run: always, since they are large."""
        return True

    def run_identity(self, path: Path) -> RunIdentity | None:
        with path.open("rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            try:
                offsets = schedtrace.offsets(mm)
            except schedtrace.FormatError:
                # Left to parse(), whose error the page shows.
                return None
            if not offsets:
                return None
            try:
                first = schedtrace.event_at(mm, offsets[0], grid_only=True)["timestamp_us"] / 1e6
                last = schedtrace.event_at(mm, offsets[-1], grid_only=True)["timestamp_us"] / 1e6
            except schedtrace.CorruptEventError:
                # The events that decode are only known once parsed, so the span is left to parse() too.
                return None
        return RunIdentity(None, first, last)

    def parse(self, path: Path, writer: DatasetWriter, progress: ProgressFn | None = None) -> None:
        with path.open("rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            offsets = schedtrace.offsets(mm)
            if not offsets:
                return
            try:
                start = schedtrace.event_at(mm, offsets[0])
            except schedtrace.CorruptEventError:
                start = {"type": None}
        cell = grid_util.cell_of(start) if start["type"] == "CellStartEvent" else None
        if cell is None:
            raise ValueError(f"{path.name}: the trace does not start with the start event of its cell.")
        size = path.stat().st_size
        # Chunks of whole events, each parsed in the pool with the record of its first event.
        workers = parallel.nof_workers()
        chunk = len(offsets) if workers == 1 else max(_MIN_CHUNK_EVENTS, -(-len(offsets) // (workers * _CHUNKS_PER_WORKER)))
        bounds = [*offsets[::chunk], offsets[-1] + 4 + _event_size(path, offsets[-1])]
        args = [(str(path), lo, hi, 1 + i * chunk, _cell_dict(cell)) for i, (lo, hi) in enumerate(zip(bounds, bounds[1:]))]
        # Bins that span chunks, merged from the parts of each.
        bins: dict[tuple[int, int], dict[str, Any]] = {}
        agg = ue_metrics.Aggregator()
        # Kinds of slot items with rows, which get a dataset.
        seen: set[str] = set()
        mixed = Counter()
        t_min = t_max = None
        for (_, _, hi, _, _), res in zip(args, parallel.map_chunks(_parse_chunk, args)):
            writer.add_record_texts(res["texts"])
            writer.add_grid_slots(res["slots"])
            writer.add_grid_cells(res["cells"])
            for kind, rows in res["items"].items():
                if rows:
                    writer.add_rows(kind, ue_metrics.ITEM_FIELDS[kind], _column_types(ue_metrics.ITEM_FIELDS[kind], rows),
                                    rows)
                    seen.add(kind)
            agg.merge(res["agg"])
            mixed.update(res["mixed"])
            for key, b in res["partial_bins"].items():
                if key in bins:
                    _merge_bin(bins, key, b)
                else:
                    bins[key] = b
            if res["t_min"] is not None:
                t_min = res["t_min"] if t_min is None else min(t_min, res["t_min"])
                t_max = res["t_max"] if t_max is None else max(t_max, res["t_max"])
            if progress:
                progress(hi, size)
            if res["corrupt"] is not None:
                # As parsers.schedtrace does, the events from a corrupt one are left out.
                warnings.warn(f"{path.name}: {res['corrupt']}, the events from it are left out.")
                break
        writer.add_grid_cells([_cells_row(key, b) for key, b in bins.items()])
        for name, fields, rows, units, context in (
            (_UE_DATASET, ue_metrics.UE_FIELDS, agg.ue_rows(), ue_metrics.UE_UNITS, ["rnti", "ue_index"]),
            (_CELL_DATASET, ue_metrics.CELL_FIELDS, agg.cell_rows(), ue_metrics.CELL_UNITS, []),
        ):
            if rows:
                writer.add_rows(name, fields, _column_types(fields, rows), rows)
                writer.set_dataset_info(name, units, context, label=_DATASET_LABELS[name], types=_number_types(fields))
        for kind in seen:
            fields = ue_metrics.ITEM_FIELDS[kind]
            units = {f: u for f, u in ue_metrics.ITEM_UNITS.items() if f in fields}
            writer.set_dataset_info(kind, units, ["rnti", "ue_index"], label=_DATASET_LABELS[kind],
                                    types=_number_types(fields))
        active = mixed["dl"] + mixed["ul"] + mixed["both"]
        duplex = "FDD" if active and mixed["both"] / active >= _FDD_MIN_SHARE else "TDD"
        writer.set_meta("grid", {**_cell_dict(cell), "nof_crbs": cell.nof_crbs, "slot_duration_s": cell.slot_duration_s,
                                 "slots_per_frame": round(0.01 / cell.slot_duration_s),
                                 "duplex": duplex, "group_size": GROUP_SIZE, "cell_id": start.get("pci")})
        if t_min is not None:
            writer.set_time_range(t_min, t_max + cell.slot_duration_s)

    def record_detail(self, path: Path, record: int) -> str:
        """The decoding of an event, as JSON."""
        return json.dumps(self.record_data(path, record), indent=1)

    def record_data(self, path: Path, record: int) -> dict[str, Any]:
        """The decoding of an event, as schedtrace.read() gives it."""
        offsets = _offsets(str(path), path.stat().st_mtime_ns)
        if not 1 <= record <= len(offsets):
            raise ValueError(f"No event {record}.")
        with path.open("rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            try:
                return schedtrace.event_at(mm, offsets[record - 1])
            except schedtrace.CorruptEventError as e:
                raise ValueError(f"Event {record} does not decode: {e}") from None

    def grid(self, path: Path, store: Any, t0: float | None, t1: float | None, width: int) -> dict[str, Any]:
        """The resource grid of a time window for a chart width in pixels, with "anchors", slots as [time, slot_tx], to
        label time in SFN.slot: its allocations ("rects"), for at most RECTS_MAX slots, else its occupancy ("cells") per
        slot, for at most SLOT_HEAT_MAX slots, else per bin of the finest stored level with at most one bin per pixel.

        The slots of the anchors, allocations and occupancy per slot are placed on a grid of slot duration by their
        slot_tx, see _slot_times(), rather than at their timestamps, which jitter with the time the events were made.
        """
        meta = store.meta["grid"]
        cell = grid_util.Cell(**{f.name: meta[f.name] for f in dataclasses.fields(grid_util.Cell)})
        duration = meta["slot_duration_s"]
        out: dict[str, Any] = {"cell": meta}
        count, rows = store.grid_slots(t0, t1, SLOT_HEAT_MAX)
        out["nof_slots"] = count
        wrap = _NOF_SFNS * meta["slots_per_frame"]
        # Slots of known slot_tx, to label the time axis in SFN.slot: all of the window, or about one per pixel.
        every = max(1, count // max(1, width))
        if count <= SLOT_HEAT_MAX:
            rects, cells = [], []
            times = _slot_times([(r[1], r[4]) for r in rows], duration, wrap)
            # From the slots drawn, so that the ticks fall on their boundaries.
            out["anchors"] = [[t, r[4]] for t, r in list(zip(times, rows))[::every]]
            with path.open("rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                for (record, _, offset, _, _), t in zip(rows, times):
                    allocs = grid_util.allocations(schedtrace.event_at(mm, offset, grid_only=True), cell)
                    if count <= RECTS_MAX:
                        rects.extend({**a, "t": t, "t1": t + duration, "record": record} for a in allocs)
                    else:
                        for (grp, channel), occ in grid_util.occupancy(allocs, GROUP_SIZE).items():
                            cells.append([t, t + duration, grp, channel, occ])
            if count <= RECTS_MAX:
                return {**out, "mode": "rects", "rects": rects}
            return {**out, "mode": "cells", "level": 1, "cells": cells}
        anchors = store.grid_anchors(t0, t1, every)
        out["anchors"] = [[t, a[2]] for t, a in zip(_slot_times([(a[0], a[2]) for a in anchors], duration, wrap), anchors)]
        level = next((lv for lv in LEVELS if count / lv <= max(1, width)), LEVELS[-1])
        return {**out, "mode": "cells", "level": level, "cells": [list(c) for c in store.grid_cells(level, t0, t1)]}


    def harq(self, path: Path, store: Any, record: int, item: tuple[str, int]) -> dict[str, Any] | None:
        """The HARQ transaction of an allocation, a PDSCH, PUSCH or PDCCH given by its event and its [list, index] in
        the slot decision, see parsers.schedtrace.harq, or None. Its rows name their event as "record", and their slot
        in SFN.slot. The events read are those within HARQ_WINDOW slots of the allocation's.
        """
        meta = store.meta["grid"]
        spf = meta["slots_per_frame"]
        offsets = _offsets(str(path), path.stat().st_mtime_ns)
        if not 1 <= record <= len(offsets):
            raise ValueError(f"No event {record}.")
        # Slot events are one per slot, after the start event of the cell.
        first, last = max(2, record - HARQ_WINDOW), min(len(offsets), record + HARQ_WINDOW)
        with path.open("rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            events = ((r, schedtrace.event_at(mm, offsets[r - 1], grid_only=True)) for r in range(first, last + 1))
            try:
                tr = harq_util.find(events, spf, record, item)
            except schedtrace.CorruptEventError as e:
                raise ValueError(str(e)) from None
        if tr is None:
            return None
        # Times of the slots as the grid places them, see _slot_times(), so that a row focuses on its slot.
        lo, hi = min(r.t for r in tr.rows), max(r.t for r in tr.rows)
        _, slots = store.grid_slots(lo - HARQ_MARGIN_S, hi + HARQ_MARGIN_S, 4 * HARQ_WINDOW)
        times = dict(zip((s[0] for s in slots), _slot_times([(s[1], s[4]) for s in slots], meta["slot_duration_s"],
                                                            _NOF_SFNS * spf)))
        return {"dir": tr.dir, "rnti": tr.rnti, "ue_index": tr.ue_index, "harq_id": tr.harq_id, "tbs": tr.tbs,
                "nof_tx": tr.nof_tx, "ended_by": tr.ended_by, "started": tr.started, "outcome": tr.outcome(),
                "rows": [{"kind": r.kind, "record": r.event, "slot": "{}.{}".format(*divmod(r.slot_tx, spf)),
                          "t": times.get(r.event, r.t),
                          "tx": r.tx, "item": list(r.item) if r.item else None, "params": r.params} for r in tr.rows]}


def _slot_times(slots: list[tuple[float, int]], duration: float, wrap: int) -> list[float]:
    """Times of slots, given as (timestamp, slot_tx) in time order, on a grid of slot duration: each at its slot count,
    slot_tx unwrapped across SFN wraps, times the duration, plus the median difference between the timestamps and
    that grid, which keeps the slots at the times of the window.
    """
    counts = []
    unwrapped = 0
    last = None
    for _, slot_tx in slots:
        if last is not None and slot_tx + unwrapped < last - wrap // 2:
            unwrapped += wrap
        last = slot_tx + unwrapped
        counts.append(last)
    if not counts:
        return []
    diffs = sorted(t - c * duration for (t, _), c in zip(slots, counts))
    offset = diffs[len(diffs) // 2]
    return [offset + c * duration for c in counts]


def _cell_dict(cell: grid_util.Cell) -> dict[str, Any]:
    return dataclasses.asdict(cell)


def _event_size(path: Path, offset: int) -> int:
    with path.open("rb") as f:
        f.seek(offset)
        return _U32.unpack(f.read(4))[0]


@lru_cache(maxsize=4)
def _offsets(path: str, mtime_ns: int) -> list[int]:
    """The event offsets of a trace, cached while the file is unchanged."""
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        return schedtrace.offsets(mm)


def _summary(event: dict[str, Any], allocs: list[dict[str, Any]], slots_per_frame: int) -> str:
    """One line of an event for the record pane, its slot in SFN.slot."""
    t = datetime.fromtimestamp(event["timestamp_us"] / 1e6, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")
    if event["type"] != "CellSlotEvent":
        return f"{t} {event['type']}"
    counts = Counter(a["channel"] for a in allocs)
    inputs = Counter(i["type"] for i in event.get("inputs") or [] if i)
    parts = [f"{n}x{ch}" for ch, n in counts.items()] + [f"{n}x{name.removesuffix('Event')}" for name, n in inputs.items()]
    sfn, slot = divmod(event["slot_tx"], slots_per_frame)
    return f"{t} slot={sfn}.{slot} latency={event['latency_us']}us " + " ".join(parts)


def _parse_chunk(path: str, start: int, end: int, first_record: int, cell_dict: dict[str, Any]) -> dict[str, Any]:
    """Parses the events of the byte range [start, end) of a trace, the first being record first_record: their texts,
    the grid slots, the occupancy of the bins of LEVELS they fall in, as rows of the bins within the range ("cells")
    and parts of the bins that span other ranges ("partial_bins"), and how many slots have DL, UL or both shared or
    control channels. A corrupt event ends the range, its error as "corrupt", else None.
    """
    cell = grid_util.Cell(**cell_dict)
    duration = cell.slot_duration_s
    spf = round(0.01 / duration)
    texts, slots = [], []
    # Bins of the finest level by index, from which the coarser levels are rolled up.
    fine: dict[int, dict[str, Any]] = {}
    mixed: Counter = Counter()
    agg = ue_metrics.Aggregator()
    items: dict[str, list[tuple]] = {kind: [] for kind in ue_metrics.ITEM_FIELDS}
    t_min = t_max = None
    corrupt = None
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        offsets = schedtrace.offsets(mm, start, end)
        for i, offset in enumerate(offsets):
            record = first_record + i
            try:
                event = schedtrace.event_at(mm, offset, grid_only=True)
            except schedtrace.CorruptEventError as e:
                corrupt = str(e).removesuffix(".")
                offsets = offsets[:i]
                break
            t = event["timestamp_us"] / 1e6
            t_min = t if t_min is None else min(t_min, t)
            t_max = t if t_max is None else max(t_max, t)
            if event["type"] != "CellSlotEvent":
                texts.append((record, _summary(event, [], spf)))
                continue
            allocs = grid_util.allocations(event, cell)
            texts.append((record, _summary(event, allocs, spf)))
            slot_items = ue_metrics.slot_items(event, cell)
            agg.add(record, event, slot_items)
            for kind, rows in items.items():
                # Booleans as 0 and 1, which plots and filters take as numbers.
                rows.extend((t, record, *(int(v) if isinstance(v, bool) else v for v in row)) for row in slot_items[kind])
            slots.append((record, t, offset, _U32.unpack_from(mm, offset)[0] + 4, event["slot_tx"]))
            dirs = {a["dir"] for a in allocs if a["channel"] in ("PDSCH", "PUSCH", "PUCCH")}
            if dirs:
                mixed["both" if len(dirs) == 2 else dirs.pop().lower()] += 1
            b = fine.get(record // LEVELS[0])
            if b is None:
                b = fine[record // LEVELS[0]] = {"t0": t, "t1": t + duration, "n": 0, "occ": {}}
            b["t0"], b["t1"] = min(b["t0"], t), max(b["t1"], t + duration)
            b["n"] += 1
            occ = b["occ"]
            for key, value in grid_util.occupancy(allocs, GROUP_SIZE).items():
                occ[key] = occ.get(key, 0.0) + value
    bins = {(LEVELS[0], index): b for index, b in fine.items()}
    finer, prev = fine, LEVELS[0]
    for level in LEVELS[1:]:
        coarser: dict[int, dict[str, Any]] = {}
        for index, b in finer.items():
            _merge_bin(coarser, index * prev // level, b)
        bins.update(((level, index), b) for index, b in coarser.items())
        finer, prev = coarser, level
    # Bins within the records of the chunk are complete, and encoded here rather than by the parent process.
    last_record = first_record + len(offsets) - 1
    cells, partial = [], {}
    for (level, index), b in bins.items():
        if index * level >= first_record and (index + 1) * level - 1 <= last_record:
            cells.append(_cells_row((level, index), b))
        else:
            partial[(level, index)] = b
    return {"texts": texts, "slots": slots, "cells": cells, "partial_bins": partial, "items": items, "agg": agg,
            "mixed": mixed, "t_min": t_min, "t_max": t_max, "corrupt": corrupt}


def _column_types(fields: tuple[str, ...], rows: list[tuple]) -> dict[str, set[str]]:
    """The column types of the values of each field of rows of time, record and the field values."""
    return {f: {t for row in rows if (t := column_type(row[i + 2])) is not None} for i, f in enumerate(fields)}


def _number_types(fields: tuple[str, ...]) -> dict[str, str]:
    """The column types of the fields of slot items and aggregates, all numbers but those of _NON_NUMBER_FIELDS."""
    return {f: "number" for f in fields if f not in _NON_NUMBER_FIELDS}


def _cells_row(key: tuple[int, int], b: dict[str, Any]) -> tuple:
    """The row of the grid cells of a bin of (level, index)."""
    cells = [[grp, channel, occ / b["n"]] for (grp, channel), occ in b["occ"].items()]
    return (*key, b["t0"], b["t1"], json.dumps(cells, separators=(",", ":")))


def _merge_bin(bins: dict[Any, dict[str, Any]], key: Any, b: dict[str, Any]) -> None:
    """Adds an occupancy bin into the bin of a key of bins."""
    into = bins.get(key)
    if into is None:
        bins[key] = {"t0": b["t0"], "t1": b["t1"], "n": b["n"], "occ": dict(b["occ"])}
        return
    into["t0"], into["t1"] = min(into["t0"], b["t0"]), max(into["t1"], b["t1"])
    into["n"] += b["n"]
    occ = into["occ"]
    for k, v in b["occ"].items():
        occ[k] = occ.get(k, 0.0) + v

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Source type for the METRICS lines of OCUDU logs."""

from __future__ import annotations

import functools
import hashlib
import itertools
import logging
import re
from datetime import timezone
from importlib import metadata
from pathlib import Path
from typing import Any

import parsers
from parsers.log import chunks, config, events, metrics, preamble
from parsers.log.run import last_time, log_run
from parsers.ran.rnti import normalize as normalize_rnti

from .. import parallel
from .base import DatasetWriter, EventWriter, ProgressFn, RunIdentity, column_type, column_value

logger = logging.getLogger("parsers")

# Fields that identify the entity a metric belongs to, besides the ones in the line context.
_IDENTITY_FIELDS = ("du", "ue", "rb", "drb")
# Display label and instance field of the datasets that are not shown by their layer name.
_DATASET_INFO = {"exec": {"label": "executors", "instance": "executor"}}
# Lines between two registered record offsets.
_OFFSET_INTERVAL = 1000
# Smallest chunk, so that logs under twice this size are parsed in the server process, where they parse as fast as
# with workers. And chunks per worker, so that a slow chunk does not hold back the following ones for long.
_MIN_CHUNK_SIZE = 4 << 20
_CHUNKS_PER_WORKER = 4
_EVENT_CANDIDATE_RE = re.compile(events.CANDIDATE_PATTERN.encode())
# Generic warnings and errors of the same logger, level, UE and message but for its numbers, each within REPEAT_GAP_S of
# the previous one, are stored as one event, up to REPEAT_MAX_SPAN_S long, with their count and span.
REPEAT_TYPES = frozenset({"warning", "error"})
REPEAT_GAP_S = 1.0
REPEAT_MAX_SPAN_S = 10.0
_NUMBERS_RE = re.compile(r"\d+")
# Log level options of the layers that hold the UE events, with the level of their lines and the events they hold.
_EVENT_LEVEL_OPTIONS = (
    ("mac", "info", "random access"),
    ("du", "info", "UE creation and deletion"),
    ("du", "warning", "RLF"),
    ("rrc", "info", "RRC messages, reestablishment"),
    ("cu", "info", "handover"),
    ("ngap", "info", "handover preparation"),
)


def _event_notes(path: Path) -> list[str]:
    """Notes about the UE events that the log levels of the log exclude, from its configuration echo."""
    with path.open(encoding="utf-8", errors="replace") as f:
        cfg = config.from_log(f)
    if cfg is None:
        return []
    quiet = [
        (option, held) for option, level, held in _EVENT_LEVEL_OPTIONS
        if config.LEVELS.index(cfg.level(option)) < config.LEVELS.index(level)
    ]
    if not quiet:
        return []
    by_level: dict[str, list[str]] = {}
    for option, _ in quiet:
        options = by_level.setdefault(cfg.level(option), [])
        if option not in options:
            options.append(option)
    levels = "; ".join(f"{', '.join(options)} at {level}" for level, options in by_level.items())
    held = ", ".join(held for _, held in quiet)
    return [f"Some UE events are not in the log, whose layers log below the level of their lines ({levels}): {held}."]


def _parse_metrics_chunk(path: str, start: int, end: int, first_line: int) -> dict[str, Any]:
    """Parses the metrics of the byte range [start, end) of a log, which starts at line first_line.

    Returns the record offsets, the first timestamp, the units seen, the warnings about units and, per layer, the rows
    grouped by their field names in order of first appearance: each group with the column types of each field and the
    rows as tuples of time, line number and column values, ready to store.
    """
    result: dict[str, Any] = {"offsets": [], "first_t": None, "units": {}, "warnings": {}, "layers": {}}
    if end <= start:
        return result
    lines = chunks.read_lines(path, start, end)
    # Warnings are returned, to be logged once across chunks.
    parser = metrics.MetricsParser(log_warnings=False)
    # Per layer, the groups of rows by field names: the field names, the column types of each field and the rows.
    layers: dict[str, dict[tuple, tuple[tuple, dict[str, set[str]], list[tuple]]]] = {}
    first_t = None
    offset = start
    for line_no, raw in enumerate(lines, start=first_line):
        if line_no % _OFFSET_INTERVAL == 1:
            result["offsets"].append((line_no, offset))
        offset += len(raw) + 1
        if first_t is None and (m := preamble.match_preamble(raw.decode("utf-8", "replace"))):
            first_t = preamble.to_epoch(m.group("timestamp"))
        # Cheap check before decoding, most lines are not metrics.
        if b"[METRICS" not in raw:
            continue
        rec = parser.parse(raw.decode("utf-8", "replace"))
        if rec is None:
            continue
        layer = rec.pop("layer")
        t = rec.pop("timestamp").replace(tzinfo=timezone.utc).timestamp()
        groups = layers.setdefault(layer, {})
        fields = tuple(rec)
        group = groups.get(fields)
        if group is None:
            group = groups[fields] = (fields, {f: set() for f in fields}, [])
        _, types, rows = group
        values = []
        for field, value in rec.items():
            vtype = column_type(value)
            if vtype is not None:
                types[field].add(vtype)
            values.append(column_value(value))
        rows.append((t, line_no, *values))

    result["first_t"] = first_t
    result["units"] = parser.units
    result["warnings"] = parser.warnings
    result["layers"] = {layer: list(groups.values()) for layer, groups in layers.items()}
    return result


def _parse_events_chunk(path: str, start: int, end: int, first_line: int) -> list[tuple]:
    """Parses the events of the byte range [start, end) of a log, which starts at line first_line and at a log entry.

    Returns them as rows of time, line number, type, category, layer, level, ue, rnti, cause, text, count and span, in
    file order, with the BINDING records of events.parse(). Repeated generic warnings and errors are one row, see
    REPEAT_TYPES, with their count and span; other rows have None.
    """
    rows: list[list] = []
    if end <= start:
        return rows
    # Header line number, line, preamble match and continuation lines of the entry whose events are pending.
    pending: tuple[int, str, re.Match, list[str]] | None = None
    # Row of the last run of each repeated warning or error, and the time of its last line.
    runs: dict[tuple, tuple[list, float]] = {}

    def add(line_no: int, line: str, m: re.Match, body: list[str]) -> None:
        for ev in events.parse(line, body, m):
            t = ev["timestamp"].replace(tzinfo=timezone.utc).timestamp()
            key = None
            if ev["type"] in REPEAT_TYPES:
                key = (ev["type"], ev["layer"], ev["level"], ev["ue"], ev["rnti"], _NUMBERS_RE.sub("#", ev["text"]))
                run = runs.get(key)
                if run is not None and t - run[1] <= REPEAT_GAP_S and t - run[0][0] <= REPEAT_MAX_SPAN_S:
                    row = run[0]
                    row[10] = (row[10] or 1) + 1
                    row[11] = t - row[0]
                    runs[key] = (row, t)
                    continue
            row = [t, line_no, ev["type"], ev["category"], ev["layer"], ev["level"], ev["ue"], ev["rnti"], ev["cause"], ev["text"], None, None]
            rows.append(row)
            if key is not None:
                runs[key] = (row, t)

    for line_no, raw in enumerate(chunks.read_lines(path, start, end), start=first_line):
        if pending is not None:
            if not chunks.ENTRY_START_RE.match(raw):
                pending[3].append(raw.decode("utf-8", "replace"))
                continue
            add(*pending)
            pending = None
        # Cheap check before decoding, most lines are not events.
        if not _EVENT_CANDIDATE_RE.search(raw):
            continue
        line = raw.decode("utf-8", "replace")
        if m := preamble.match_preamble(line):
            if events.has_body(m):
                pending = (line_no, line, m, [])
            else:
                add(line_no, line, m, [])
    if pending is not None:
        add(*pending)
    return rows


def _chunk_ranges(path: Path) -> list[tuple[int, int]]:
    """Byte ranges of a log, starting at log entries, to parse in the pool: one when it has a single worker or the log
    is small.
    """
    workers = parallel.nof_workers()
    nof_chunks = 1 if workers == 1 else max(1, min(workers * _CHUNKS_PER_WORKER, path.stat().st_size // _MIN_CHUNK_SIZE))
    return chunks.chunk_ranges(path, nof_chunks)


@functools.lru_cache(maxsize=1)
def _parsers_version() -> str:
    """Version of the parsers package, with a hash of its sources, since its version number is not bumped on changes."""
    try:
        version = metadata.version("ocudu-parsers")
    except metadata.PackageNotFoundError:
        version = "unknown"
    h = hashlib.sha256()
    root = Path(parsers.__file__).parent
    for path in sorted(root.rglob("*.py")):
        h.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes())
    return f"{version}-{h.hexdigest()[:12]}"


# Bytes of the start and of the end of a file that accepts() looks for log entries in.
_ACCEPT_BYTES = 64 << 10


class LogMetricsSource:
    """One time series dataset per METRICS layer, and the events of the log. Records are log lines, identified by
    line number.
    """

    name = "log_metrics"
    version = f"9+parsers-{_parsers_version()}"

    def accepts(self, path: Path) -> bool:
        """Files with a log entry in their first 200 lines or 64 KB or in their last 64 KB, and empty .log files."""
        # The end tells excerpts that start within the configuration echo, and empty logs are of runs that wrote
        # nothing yet.
        size = _ACCEPT_BYTES
        try:
            with path.open("rb") as f:
                head = f.read(size)
                if not head:
                    return path.suffix == ".log"
                tail = b""
                if f.seek(0, 2) > size:
                    f.seek(-size, 2)
                    # Without its first line, which may be cut.
                    tail = f.read(size).partition(b"\n")[2]
        except OSError:
            return False
        if b"\0" in head:
            return False
        lines = head.decode("utf-8", "replace").splitlines()[:200]
        return any(preamble.match_preamble(line) for line in lines) or any(
            preamble.match_preamble(line) for line in tail.decode("utf-8", "replace").splitlines()
        )

    def parse(self, path: Path, writer: DatasetWriter, progress: ProgressFn | None = None) -> None:
        writer.set_notes(_event_notes(path))
        total = path.stat().st_size
        ranges = _chunk_ranges(path)
        # Counted in the pool, since counting the lines of a large file takes about a second.
        counts = parallel.map_chunks(chunks.count_lines, [(str(path), start, end) for start, end in ranges[:-1]])
        first_lines = [1 + n for n in itertools.accumulate(counts, initial=0)]
        args = [(str(path), start, end, first_line) for (start, end), first_line in zip(ranges, first_lines)]
        results = parallel.map_chunks(_parse_metrics_chunk, args)

        # Results come in file order, so units seen first win as in a single pass.
        all_units: dict[str, dict[str, str]] = {layer: {} for layer in metrics.LAYER_PATTERNS}
        warned: set[tuple[str, str]] = set()
        first_t = None
        for (_, end), res in zip(ranges, results):
            for line_no, offset in res["offsets"]:
                writer.add_record_offset(line_no, offset)
            for layer, groups in res["layers"].items():
                for fields, types, rows in groups:
                    writer.add_rows(layer, fields, types, rows)
            for key, msg in res["warnings"].items():
                if key not in warned:
                    warned.add(key)
                    logger.warning(msg)
            for layer, chunk_units in res["units"].items():
                for field, unit in chunk_units.items():
                    known = all_units[layer].setdefault(field, unit)
                    if known != unit and (layer, field) not in warned:
                        warned.add((layer, field))
                        logger.warning(f"Field {layer}.{field} has unit {unit!r}, expected {known!r}.")
            if first_t is None:
                first_t = res["first_t"]
            if progress:
                progress(end, total)

        last_t = last_time(path)
        if first_t is not None and last_t is not None:
            writer.set_time_range(first_t, last_t)
        for layer, units in all_units.items():
            context = list(dict.fromkeys([*metrics.LAYER_PATTERNS[layer].groupindex, *_IDENTITY_FIELDS]))
            writer.set_dataset_info(layer, units, context, **_DATASET_INFO.get(layer, {}))

    def field_spans(self, text: str) -> dict[str, tuple[int, int]]:
        return metrics.field_spans(text)

    def run_identity(self, path: Path) -> RunIdentity | None:
        r = log_run(path)
        return RunIdentity((r.mode, r.commit, r.branch), r.start, r.end) if r else None

    def parse_events(self, path: Path, writer: EventWriter) -> None:
        # Chunks are parsed in the pool and their events assigned to UE contexts here, in file order, since the
        # contexts of a chunk depend on the events before it.
        ranges = _chunk_ranges(path)
        counts = parallel.map_chunks(chunks.count_lines, [(str(path), start, end) for start, end in ranges[:-1]])
        first_lines = [1 + n for n in itertools.accumulate(counts, initial=0)]
        args = [(str(path), start, end, first_line) for (start, end), first_line in zip(ranges, first_lines)]
        tracker = events.UeTracker()
        for rows in parallel.map_chunks(_parse_events_chunk, args):
            out = []
            for row in rows:
                t, _, type_, category, layer, _, ue, rnti, _, text, count, span = row
                lane = tracker.assign_ids(t, type_, category, layer, ue, rnti)
                if category != events.BINDING:
                    # Logs print small RNTIs short, e.g. 0x44, which other sources give as 0x0044.
                    out.append((*row[:7], normalize_rnti(rnti), *row[8:10], lane, count, span))
            writer.add_event_rows(out)
        for lane in tracker.lanes:
            writer.add_lane(lane.id, lane.du_ue, normalize_rnti(lane.rnti), cu_ue=lane.cu_ue)

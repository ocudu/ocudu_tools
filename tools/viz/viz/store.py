# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""SQLite store of the datasets of a source: parse cache and query engine."""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import os
import sqlite3
import tempfile
import urllib.parse
import uuid
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from .filters import FilterError, compile_filter
from .sources.base import ProgressFn, SourceType, column_type, column_value

# Bumped when the database layout changes, part of the cache key.
SCHEMA_VERSION = 9
# Time series windows up to this many points are returned at full resolution.
MAX_FULL_RES_POINTS = 50_000
# Split values returned when none are selected.
MAX_DEFAULT_SPLITS = 256
# Split values whose statistics are returned when none are selected, the first of the plotted ones.
MAX_STATS_SPLITS = 16
# Columns of the events table, after the timestamp and record id.
# count and span are the lines and the seconds from the first to the last of an event that stands for repeated ones,
# else None.
EVENT_COLUMNS = ("type", "category", "layer", "level", "ue", "rnti", "cause", "text", "lane", "count", "span")
_EVENT_COLUMN_TYPES = {"ue": "INTEGER", "lane": "INTEGER", "count": "INTEGER", "span": "REAL"}
# Events returned by default for a time window.
DEFAULT_MAX_EVENTS = 5000
# UE lanes returned by default for a trace window.
DEFAULT_MAX_LANES = 300
# Rows returned by default for a table.
DEFAULT_TABLE_ROWS = 1000
# Bins of a histogram, unless the field has few integer values.
DEFAULT_HISTOGRAM_BINS = 50
# Windows with more points get percentiles from a sample.
MAX_EXACT_PERCENTILE_POINTS = 2_000_000

# Reserved column names of dataset tables.
_TS = "_ts"
_REC = "_rec"
# Rows buffered per dataset before an insert.
_BATCH_SIZE = 5000
_CACHE_SUFFIX = ".sqlite"
# Suffix of the cache files of the events, built after the datasets.
_EVENTS_SUFFIX = ".events" + _CACHE_SUFFIX

_memory_db_ids = itertools.count()


class QueryError(ValueError):
    """Invalid query arguments, e.g. an unknown dataset or field."""


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class _DatasetBuffer:
    """Columns and pending rows of one dataset table being built."""

    def __init__(self, name: str):
        self.name = name
        self.columns: list[str] = []
        self.types: dict[str, str] = {}
        self.rows: list[tuple] = []


class StoreWriter:
    """Writes the datasets of a source into a SQLite database."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._datasets: dict[str, _DatasetBuffer] = {}
        self._info: dict[str, tuple[dict[str, str], list[str], str | None, str | None, dict[str, str]]] = {}
        self._time_range: tuple[float, float] | None = None
        self._notes: list[str] = []
        # Source-specific metadata, e.g. the cell of a resource grid.
        self._extra_meta: dict[str, Any] = {}
        # Whether the resource grid tables were created, and the table of the lines that are the records.
        self._grid = False
        self._lines = False
        self._conn.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE datasets (
                name TEXT PRIMARY KEY, kind TEXT, fields TEXT, units TEXT, context TEXT, label TEXT, instance TEXT
            );
            CREATE TABLE record_offsets (record INTEGER PRIMARY KEY, offset INTEGER);
            CREATE TABLE record_texts (record INTEGER PRIMARY KEY, text TEXT);
            """
        )

    def add_row(self, dataset: str, record: int, t: float, fields: dict[str, Any]) -> None:
        ds = self._dataset(dataset, fields)
        row = [t, record]
        for col in ds.columns:
            value = fields.get(col)
            vtype = column_type(value)
            if vtype is not None:
                self._merge_type(ds, col, vtype)
            row.append(column_value(value))
        ds.rows.append(tuple(row))
        if len(ds.rows) >= _BATCH_SIZE:
            self._flush(ds)

    def add_rows(self, dataset: str, fields: Sequence[str], types: dict[str, set[str]], rows: list[tuple]) -> None:
        ds = self._dataset(dataset, fields)
        for col, col_types in types.items():
            for vtype in col_types:
                self._merge_type(ds, col, vtype)
        # Buffered rows go first, to keep the rows in the order they were added.
        self._flush(ds)
        cols = ", ".join([_TS, _REC] + [_quote(c) for c in fields])
        marks = ", ".join("?" * (len(fields) + 2))
        self._conn.executemany(f"INSERT INTO {_quote('ds_' + ds.name)} ({cols}) VALUES ({marks})", rows)
    def set_dataset_info(
        self,
        dataset: str,
        units: dict[str, str],
        context: list[str],
        label: str | None = None,
        instance: str | None = None,
        types: dict[str, str] | None = None,
    ) -> None:
        self._info[dataset] = (units, context, label, instance, types or {})

    def set_time_range(self, t_min: float, t_max: float) -> None:
        self._time_range = (t_min, t_max)

    def set_notes(self, notes: list[str]) -> None:
        self._notes = list(notes)

    def add_record_offset(self, record: int, offset: int) -> None:
        self._conn.execute("INSERT OR REPLACE INTO record_offsets VALUES (?, ?)", (record, offset))

    def add_record_text(self, record: int, text: str) -> None:
        self._conn.execute("INSERT OR REPLACE INTO record_texts VALUES (?, ?)", (record, text))

    def add_record_texts(self, rows: list[tuple[int, str]]) -> None:
        self._conn.executemany("INSERT OR REPLACE INTO record_texts VALUES (?, ?)", rows)

    def add_record_lines(self, rows: list[tuple[int, int]]) -> None:
        """Makes the records the lines of the file at these (line number, offset), rather than all its lines."""
        if not self._lines:
            self._lines = True
            self._conn.execute("CREATE TABLE record_lines (record INTEGER PRIMARY KEY, offset INTEGER)")
        self._conn.executemany("INSERT OR REPLACE INTO record_lines VALUES (?, ?)", rows)

    def set_meta(self, key: str, value: Any) -> None:
        self._extra_meta[key] = value

    def add_grid_slots(self, rows: list[tuple]) -> None:
        if not self._grid:
            self._create_grid()
        self._conn.executemany("INSERT INTO grid_slots VALUES (?, ?, ?, ?, ?)", rows)

    def add_grid_cells(self, rows: list[tuple]) -> None:
        if not self._grid:
            self._create_grid()
        self._conn.executemany("INSERT INTO grid_cells VALUES (?, ?, ?, ?, ?)", rows)

    def _create_grid(self) -> None:
        self._grid = True
        self._conn.executescript(
            """
            CREATE TABLE grid_slots (record INTEGER PRIMARY KEY, t REAL, offset INTEGER, size INTEGER, slot_tx INTEGER);
            CREATE TABLE grid_cells (level INTEGER, bin INTEGER, t0 REAL, t1 REAL, cells TEXT);
            """
        )

    def finish(self, source_path: Path, source_type: str) -> None:
        """Flushes pending rows, writes the dataset metadata and indexes."""
        t_min = t_max = None
        for ds in self._datasets.values():
            self._flush(ds)
            table = _quote("ds_" + ds.name)
            self._conn.execute(f"CREATE INDEX {_quote('idx_' + ds.name)} ON {table} ({_TS})")
            lo, hi = self._conn.execute(f"SELECT MIN({_TS}), MAX({_TS}) FROM {table}").fetchone()
            if lo is not None:
                t_min = lo if t_min is None else min(t_min, lo)
                t_max = hi if t_max is None else max(t_max, hi)

            units, context, label, instance, types = self._info.get(ds.name, ({}, [], None, None, {}))
            # Fields without values, e.g. RSRP without reports, keep their type, so that they stay plottable.
            fields = {
                col: ds.types[col] or types.get(col) or ("number" if col in units else "text") for col in ds.columns
            }
            self._conn.execute(
                "INSERT INTO datasets VALUES (?, 'timeseries', ?, ?, ?, ?, ?)",
                (
                    ds.name,
                    json.dumps(fields),
                    json.dumps(units),
                    json.dumps([c for c in context if c in fields]),
                    label or ds.name,
                    instance if instance in fields else None,
                ),
            )

        if self._time_range is not None:
            t_min = self._time_range[0] if t_min is None else min(t_min, self._time_range[0])
            t_max = self._time_range[1] if t_max is None else max(t_max, self._time_range[1])
        meta = {
            "path": str(source_path),
            "source_type": source_type,
            "t_min": t_min,
            "t_max": t_max,
            "notes": self._notes,
            **self._extra_meta,
            "complete": True,
        }
        if self._grid:
            self._conn.execute("CREATE INDEX idx_grid_slots ON grid_slots (t)")
            self._conn.execute("CREATE INDEX idx_grid_cells ON grid_cells (level, t0)")
        self._conn.executemany("INSERT INTO meta VALUES (?, ?)", [(k, json.dumps(v)) for k, v in meta.items()])
        self._conn.commit()

    def _dataset(self, dataset: str, fields: Iterable[str]) -> _DatasetBuffer:
        """Returns the table of a dataset, created if needed, with columns for the given fields."""
        ds = self._datasets.get(dataset)
        if ds is None:
            ds = self._datasets[dataset] = _DatasetBuffer(dataset)
            self._conn.execute(f"CREATE TABLE {_quote('ds_' + dataset)} ({_TS} REAL, {_REC} INTEGER)")
        new_columns = [k for k in fields if k not in ds.types]
        if new_columns:
            self._flush(ds)
            for col in new_columns:
                ds.columns.append(col)
                ds.types[col] = ""
                self._conn.execute(f"ALTER TABLE {_quote('ds_' + dataset)} ADD COLUMN {_quote(col)}")
        return ds

    @staticmethod
    def _merge_type(ds: _DatasetBuffer, col: str, vtype: str) -> None:
        """Widens the type of a column to text when its values have different types."""
        if ds.types[col] != vtype:
            ds.types[col] = vtype if ds.types[col] == "" else "text"

    def _flush(self, ds: _DatasetBuffer) -> None:
        if not ds.rows:
            return
        cols = ", ".join([_TS, _REC] + [_quote(c) for c in ds.columns])
        marks = ", ".join("?" * (len(ds.columns) + 2))
        self._conn.executemany(f"INSERT INTO {_quote('ds_' + ds.name)} ({cols}) VALUES ({marks})", ds.rows)
        ds.rows.clear()


class EventStoreWriter:
    """Writes the events of a source into a SQLite database."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._events: list[tuple] = []
        cols = ", ".join(f"{c} {_EVENT_COLUMN_TYPES.get(c, 'TEXT')}" for c in EVENT_COLUMNS)
        self._conn.executescript(
            f"""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE events ({_TS} REAL, {_REC} INTEGER, {cols});
            CREATE TABLE lane_info (lane INTEGER PRIMARY KEY, ue INTEGER, rnti TEXT, label TEXT, cu_ue INTEGER, links TEXT);
            """
        )

    def add_event(self, record: int, t: float, event: dict[str, Any]) -> None:
        self._events.append((t, record, *(event.get(c) for c in EVENT_COLUMNS)))
        if len(self._events) >= _BATCH_SIZE:
            self._flush()

    def add_event_rows(self, rows: list[tuple]) -> None:
        self._events.extend(rows)
        if len(self._events) >= _BATCH_SIZE:
            self._flush()

    def add_lane(self, lane: int, ue: int | None, rnti: str | None, label: str | None = None, cu_ue: int | None = None,
                 links: dict[str, Any] | None = None) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO lane_info VALUES (?, ?, ?, ?, ?, ?)",
            (lane, ue, rnti, label, cu_ue, json.dumps(links) if links else None),
        )

    def finish(self) -> None:
        """Flushes pending events, writes the UE lanes and the indexes."""
        self._flush()
        self._conn.execute(f"CREATE INDEX idx_events ON events ({_TS})")
        # A lane spans the events of one UE context; created but never deleted ones last until the end of the log. A
        # failed creation leaves no context; its lane is failed.
        self._conn.execute(
            f"""
            CREATE TABLE lanes AS SELECT
                e.lane,
                MIN(e.{_TS}) AS t_start,
                MAX(e.{_TS} + COALESCE(e.span, 0)) AS t_end,
                i.ue,
                i.rnti,
                i.label,
                i.cu_ue,
                MAX(e.type = 'ue_create') AND NOT MAX(e.type IN ('ue_delete', 'ue_create_failed')) AS open,
                MAX(e.type = 'ue_create_failed') AS failed
            FROM events e LEFT JOIN lane_info i ON i.lane = e.lane WHERE e.lane IS NOT NULL GROUP BY e.lane
            """
        )
        self._conn.execute("CREATE INDEX idx_lanes ON lanes (t_start)")
        self._conn.execute("INSERT INTO meta VALUES ('complete', 'true')")
        self._conn.commit()

    def _flush(self) -> None:
        if self._events:
            marks = ", ".join("?" * (len(EVENT_COLUMNS) + 2))
            self._conn.executemany(f"INSERT INTO events VALUES ({marks})", self._events)
            self._events.clear()


class Store:
    """Read access to the datasets of one source. Safe to use from several threads."""

    def __init__(self, uri: str, keepalive: sqlite3.Connection | None = None):
        """Opens the database at the SQLite URI. keepalive holds an in-memory database open."""
        self._uri = uri
        self._keepalive = keepalive
        # Database of the events, set once they are parsed, and the connection holding it open when in memory.
        self._events_uri: str | None = None
        self._events_keepalive: sqlite3.Connection | None = None
        self.events_ready = False
        # Event count per category and per type, empty until the events are parsed.
        self.event_counts: dict[str, int] = {}
        self.event_type_counts: dict[str, int] = {}
        # Longest span of the events, see EVENT_COLUMNS.
        self._max_event_span = 0.0
        with self._connect() as conn:
            self.meta = {k: json.loads(v) for k, v in conn.execute("SELECT key, value FROM meta")}
            self.datasets = {
                name: {
                    "name": name,
                    "kind": kind,
                    "fields": json.loads(fields),
                    "units": json.loads(units),
                    "context": json.loads(context),
                    "label": label,
                    "instance": instance,
                }
                for name, kind, fields, units, context, label, instance in conn.execute(
                    "SELECT name, kind, fields, units, context, label, instance FROM datasets ORDER BY label"
                )
            }
            # Records that are some lines of the file only, e.g. those of an ad-hoc metric.
            self._sparse = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'record_lines'").fetchone() is not None
            # Time span of each dataset, from the indexed timestamp column.
            for name, ds in self.datasets.items():
                ds["t_min"], ds["t_max"] = conn.execute(f"SELECT MIN({_TS}), MAX({_TS}) FROM {_quote('ds_' + name)}").fetchone()

    @property
    def path(self) -> Path:
        return Path(self.meta["path"])

    def set_events(self, uri: str | None, keepalive: sqlite3.Connection | None = None) -> None:
        """Serves the events of the database at the SQLite URI, or no events for None."""
        counts: dict[str, int] = {}
        type_counts: dict[str, int] = {}
        max_span = 0.0
        if uri is not None:
            with closing(sqlite3.connect(uri, uri=True, check_same_thread=False)) as conn:
                # Events that stand for repeated ones count each.
                counts = dict(conn.execute("SELECT category, SUM(COALESCE(count, 1)) FROM events GROUP BY category"))
                type_counts = dict(conn.execute("SELECT type, SUM(COALESCE(count, 1)) FROM events GROUP BY type"))
                max_span = conn.execute("SELECT COALESCE(MAX(span), 0) FROM events").fetchone()[0]
        self._max_event_span = max_span
        self._events_uri, self._events_keepalive = uri, keepalive
        self.event_counts, self.event_type_counts = counts, type_counts
        self.events_ready = True

    def series(
        self,
        dataset: str,
        field: str,
        t0: float | None = None,
        t1: float | None = None,
        width: int = 1000,
        split_by: str | None = None,
        split_values: list[str] | None = None,
        filter_expr: str | None = None,
        instance: str | None = None,
        max_points: int = MAX_FULL_RES_POINTS,
        max_splits: int = MAX_DEFAULT_SPLITS,
    ) -> dict[str, Any]:
        """Returns the series of a numeric field, one per split value, downsampled to min/max per pixel if needed.

        Without explicit split values, only the first max_splits are returned.
        """
        ds = self._dataset(dataset)
        table = _quote("ds_" + dataset)
        value = _quote(field)
        split_col = _quote(split_by) if split_by else "NULL"
        with self._connect() as conn:
            where_sql, params, total_splits = self._window(conn, ds, field, t0, t1, split_by, split_values, filter_expr, instance, max_splits)
            count, lo, hi = conn.execute(f"SELECT COUNT(*), MIN({_TS}), MAX({_TS}) FROM {table} WHERE {where_sql}", params).fetchone()
            downsampled = count > max_points and hi is not None and hi > lo
            if not downsampled:
                rows = conn.execute(
                    f"SELECT {split_col}, {_TS}, {value}, {_REC} FROM {table} WHERE {where_sql} ORDER BY {_TS}, {_REC}", params
                ).fetchall()
            else:
                start = t0 if t0 is not None else lo
                end = t1 if t1 is not None else hi
                scale = width / max(end - start, 1e-9)
                # The window end would otherwise get a bucket of its own.
                bucket = f"MIN(CAST(({_TS} - ?) * ? AS INTEGER), {int(width) - 1})"
                rows = []
                # SQLite returns the other columns of the row holding the MIN or MAX.
                for agg in ("MIN", "MAX"):
                    rows += conn.execute(
                        f"SELECT {split_col}, {_TS}, {agg}({value}), {_REC} FROM {table} WHERE {where_sql} "
                        f"GROUP BY {split_col}, {bucket}",
                        params + [start, scale],
                    ).fetchall()

        by_split: dict[Any, dict[int, tuple[float, Any]]] = {}
        for g, t, v, rec in rows:
            by_split.setdefault(g, {})[rec] = (t, v)
        series = []
        for g in sorted(by_split, key=_split_sort_key):
            points = sorted(by_split[g].items(), key=lambda item: (item[1][0], item[0]))
            series.append({
                "label": _label(field, split_by, g),
                "split": g,
                "t": [p[1][0] for p in points],
                "v": [p[1][1] for p in points],
                "record": [p[0] for p in points],
            })
        return {"unit": ds["units"].get(field), "downsampled": downsampled, "total_splits": total_splits, "series": series}

    def series_rows(
        self,
        dataset: str,
        field: str,
        t0: float | None = None,
        t1: float | None = None,
        split_by: str | None = None,
        split_values: list[str] | None = None,
        filter_expr: str | None = None,
        instance: str | None = None,
        max_splits: int = MAX_DEFAULT_SPLITS,
    ) -> Iterator[tuple[Any, ...]]:
        """Yields the samples of the series of series() at full resolution, as (time, record, split value, value), in
        time order. The split value is None without split_by.
        """
        ds = self._dataset(dataset)
        split_col = _quote(split_by) if split_by else "NULL"
        with self._connect() as conn:
            where_sql, params, _ = self._window(conn, ds, field, t0, t1, split_by, split_values, filter_expr, instance, max_splits)
            yield from conn.execute(
                f"SELECT {_TS}, {_REC}, {split_col}, {_quote(field)} FROM {_quote('ds_' + dataset)} WHERE {where_sql} "
                f"ORDER BY {_TS}, {_REC}",
                params,
            )

    def stats(
        self,
        dataset: str,
        field: str,
        t0: float | None = None,
        t1: float | None = None,
        split_by: str | None = None,
        split_values: list[str] | None = None,
        filter_expr: str | None = None,
        instance: str | None = None,
        max_splits: int = MAX_STATS_SPLITS,
        max_exact_points: int = MAX_EXACT_PERCENTILE_POINTS,
    ) -> dict[str, Any]:
        """Returns count, min, max, mean and percentiles of a numeric field per split value.

        Percentiles are estimated from an evenly spaced sample when there are more than max_exact_points.
        """
        ds = self._dataset(dataset)
        table = _quote("ds_" + dataset)
        value = _quote(field)
        split_col = _quote(split_by) if split_by else "NULL"
        with self._connect() as conn:
            where_sql, params, total_splits = self._window(conn, ds, field, t0, t1, split_by, split_values, filter_expr, instance, max_splits)
            aggregates = conn.execute(
                f"SELECT {split_col}, COUNT(*), MIN({value}), MAX({value}), AVG({value}) FROM {table} "
                f"WHERE {where_sql} GROUP BY {split_col}",
                params,
            ).fetchall()
            total = sum(row[1] for row in aggregates)
            stride = -(-total // max_exact_points) if total > max_exact_points else 1
            values: dict[Any, list[float]] = {}
            sample_sql = f"SELECT {split_col}, {value} FROM {table} WHERE {where_sql}"
            # Record ids are dense enough among metric rows for a stride to be an even sample.
            sample_params = params
            if stride > 1:
                sample_sql += f" AND {_REC} % ? = 0"
                sample_params = params + [stride]
            for g, v in conn.execute(sample_sql, sample_params):
                values.setdefault(g, []).append(v)

        series = []
        for g, count, vmin, vmax, mean in sorted(aggregates, key=lambda row: _split_sort_key(row[0])):
            pct = _percentiles(values.get(g, []), (50, 95, 99))
            series.append({
                "label": _label(field, split_by, g),
                "split": g,
                "count": count,
                "min": vmin,
                "max": vmax,
                "mean": mean,
                "p50": pct[0],
                "p95": pct[1],
                "p99": pct[2],
            })
        return {"unit": ds["units"].get(field), "sampled": stride > 1, "total_splits": total_splits, "series": series}

    def histogram(
        self,
        dataset: str,
        field: str,
        t0: float | None = None,
        t1: float | None = None,
        split_by: str | None = None,
        split_values: list[str] | None = None,
        filter_expr: str | None = None,
        instance: str | None = None,
        bins: int = DEFAULT_HISTOGRAM_BINS,
        max_splits: int = MAX_DEFAULT_SPLITS,
    ) -> dict[str, Any]:
        """Returns the value distribution of a numeric field per split value, over shared bins.

        Integer fields spanning at most bins values get one bin per integer.
        """
        ds = self._dataset(dataset)
        table = _quote("ds_" + dataset)
        value = _quote(field)
        split_col = _quote(split_by) if split_by else "NULL"
        with self._connect() as conn:
            where_sql, params, total_splits = self._window(conn, ds, field, t0, t1, split_by, split_values, filter_expr, instance, max_splits)
            lo, hi, nof_fractional = conn.execute(
                f"SELECT MIN({value}), MAX({value}), SUM({value} != CAST({value} AS INTEGER)) FROM {table} WHERE {where_sql}",
                params,
            ).fetchone()
            if lo is None:
                return {"unit": ds["units"].get(field), "edges": [], "total_splits": total_splits, "series": []}
            if not nof_fractional and hi - lo + 1 <= bins:
                start, bin_width, nof_bins = lo - 0.5, 1.0, int(hi - lo) + 1
            elif hi > lo:
                start, bin_width, nof_bins = lo, (hi - lo) / bins, bins
            else:
                start, bin_width, nof_bins = lo - 0.5, 1.0, 1
            rows = conn.execute(
                f"SELECT {split_col}, MIN(CAST(({value} - ?) / ? AS INTEGER), {nof_bins - 1}), COUNT(*) FROM {table} "
                f"WHERE {where_sql} GROUP BY 1, 2",
                [start, bin_width] + params,
            ).fetchall()

        counts: dict[Any, list[int]] = {}
        for split, b, n in rows:
            counts.setdefault(split, [0] * nof_bins)[b] += n
        series = [
            {"label": _label(field, split_by, split), "split": split, "counts": counts[split]}
            for split in sorted(counts, key=_split_sort_key)
        ]
        edges = [start + i * bin_width for i in range(nof_bins + 1)]
        return {"unit": ds["units"].get(field), "edges": edges, "total_splits": total_splits, "series": series}

    def table_rows(
        self,
        dataset: str,
        t0: float | None = None,
        t1: float | None = None,
        filter_expr: str | None = None,
        instance: str | None = None,
        fields: list[str] | None = None,
        limit: int | None = DEFAULT_TABLE_ROWS,
    ) -> Iterator[tuple[Any, ...]]:
        """Yields the rows of a dataset as (time, record, *field values), in time order.

        Fields default to all the fields of the dataset. List values are JSON text. A limit of None yields all rows.
        """
        ds = self._dataset(dataset)
        columns = self._table_columns(ds, fields)
        with self._connect() as conn:
            where_sql, params, _ = self._window(conn, ds, None, t0, t1, None, None, filter_expr, instance, 0)
            selected = ", ".join([_TS, _REC] + [_quote(c) for c in columns])
            sql = f"SELECT {selected} FROM {_quote('ds_' + dataset)} WHERE {where_sql} ORDER BY {_TS}, {_REC}"
            if limit is not None:
                sql += " LIMIT ?"
                params = params + [limit]
            yield from conn.execute(sql, params)

    def count_table_rows(
        self,
        dataset: str,
        t0: float | None = None,
        t1: float | None = None,
        filter_expr: str | None = None,
        instance: str | None = None,
    ) -> int:
        """Returns the number of rows that table_rows() would yield without a limit."""
        ds = self._dataset(dataset)
        with self._connect() as conn:
            where_sql, params, _ = self._window(conn, ds, None, t0, t1, None, None, filter_expr, instance, 0)
            return conn.execute(f"SELECT COUNT(*) FROM {_quote('ds_' + dataset)} WHERE {where_sql}", params).fetchone()[0]

    def table_columns(self, dataset: str, fields: list[str] | None = None) -> list[str]:
        """Returns the columns of a table: the given fields, or all fields with the context fields first."""
        return self._table_columns(self._dataset(dataset), fields)

    def events(
        self,
        t0: float | None = None,
        t1: float | None = None,
        categories: list[str] | None = None,
        ue: int | None = None,
        limit: int = DEFAULT_MAX_EVENTS,
        types: list[str] | None = None,
    ) -> dict[str, Any]:
        """Returns the events of a time window in time order, optionally of some categories or types, or of one UE.

        With both categories and types, events of either are returned. At most limit events are returned; total counts
        all the matching ones.
        """
        if self._events_uri is None or (categories is not None and not categories and not types):
            return {"total": 0, "truncated": False, "events": []}
        where, params = ["1"], []
        if t0 is not None:
            where_t0, params_t0 = self._events_from(t0)
            where.append(where_t0)
            params.extend(params_t0)
        if t1 is not None:
            where.append(f"{_TS} <= ?")
            params.append(t1)
        if categories is not None or types:
            kinds = []
            if categories:
                kinds.append(f"category IN ({', '.join('?' * len(categories))})")
                params.extend(categories)
            if types:
                kinds.append(f"type IN ({', '.join('?' * len(types))})")
                params.extend(types)
            where.append(f"({' OR '.join(kinds)})")
        if ue is not None:
            where.append("ue = ?")
            params.append(ue)
        where_sql = " AND ".join(where)
        with closing(sqlite3.connect(self._events_uri, uri=True, check_same_thread=False)) as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM events WHERE {where_sql}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT {_TS}, {_REC}, {', '.join(EVENT_COLUMNS)} FROM events WHERE {where_sql} ORDER BY {_TS}, {_REC} LIMIT ?",
                params + [limit],
            ).fetchall()
        events = [dict(zip(("t", "record", *EVENT_COLUMNS), r)) for r in rows]
        return {"total": total, "truncated": total > len(events), "events": events}

    def trace(
        self,
        t0: float | None = None,
        t1: float | None = None,
        max_lanes: int = DEFAULT_MAX_LANES,
        limit: int = DEFAULT_MAX_EVENTS,
    ) -> dict[str, Any]:
        """Returns the UE lanes active in a time window, in start order, and the events of the window in those lanes
        or in no lane.

        Open lanes have no deletion and last until the end of the log. Failed lanes are of a failed UE creation. At most max_lanes lanes and limit events are
        returned; total_lanes and total_events count all of them.
        """
        empty = {"lanes": [], "total_lanes": 0, "events": [], "total_events": 0, "truncated": False}
        if self._events_uri is None:
            return empty
        lo = -float("inf") if t0 is None else t0
        hi = float("inf") if t1 is None else t1
        with closing(sqlite3.connect(self._events_uri, uri=True, check_same_thread=False)) as conn:
            where = "t_start <= ? AND (t_end >= ? OR open)"
            total_lanes = conn.execute(f"SELECT COUNT(*) FROM lanes WHERE {where}", (hi, lo)).fetchone()[0]
            lanes = [
                {"lane": r[0], "t_start": r[1], "t_end": r[2], "ue": r[3], "rnti": r[4], "label": r[5], "cu_ue": r[6], "open": bool(r[7]),
                 "failed": bool(r[8])}
                for r in conn.execute(
                    f"SELECT lane, t_start, t_end, ue, rnti, label, cu_ue, open, failed FROM lanes WHERE {where} ORDER BY t_start, lane LIMIT ?",
                    (hi, lo, max_lanes),
                )
            ]
            ids = [lane["lane"] for lane in lanes]
            in_lanes = f"(lane IS NULL OR lane IN ({', '.join('?' * len(ids))}))"
            where_t0, params_t0 = self._events_from(lo)
            params = [*params_t0, hi, *ids]
            event_where = f"{where_t0} AND {_TS} <= ? AND {in_lanes}"
            total_events = conn.execute(f"SELECT COUNT(*) FROM events WHERE {event_where}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT {_TS}, {_REC}, {', '.join(EVENT_COLUMNS)} FROM events WHERE {event_where} ORDER BY {_TS}, {_REC} LIMIT ?",
                params + [limit],
            ).fetchall()
        events = [dict(zip(("t", "record", *EVENT_COLUMNS), r)) for r in rows]
        return {
            "lanes": lanes,
            "total_lanes": total_lanes,
            "events": events,
            "total_events": total_events,
            "truncated": total_events > len(events),
        }

    def _events_from(self, t0: float) -> tuple[str, list[float]]:
        """WHERE clause and params of the events that end at t0 or later, repeated ones lasting their span."""
        # The bound on the start keeps the timestamp index in use.
        return f"{_TS} >= ? AND {_TS} + COALESCE(span, 0) >= ?", [t0 - self._max_event_span, t0]

    def grid_slots(self, t0: float | None, t1: float | None, limit: int) -> tuple[int, list[tuple[int, float, int, int, int]]]:
        """The number of resource grid slots in a time window and the first limit of them, as (record, time, file
        offset, size, slot_tx), in time order.
        """
        lo = -float("inf") if t0 is None else t0
        hi = float("inf") if t1 is None else t1
        with self._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM grid_slots WHERE t >= ? AND t <= ?", (lo, hi)).fetchone()[0]
            rows = conn.execute(
                "SELECT record, t, offset, size, slot_tx FROM grid_slots WHERE t >= ? AND t <= ? ORDER BY t LIMIT ?",
                (lo, hi, limit),
            ).fetchall()
        return count, rows

    def grid_anchors(self, t0: float | None, t1: float | None, every: int) -> list[tuple[float, int, int]]:
        """Every every-th resource grid slot of a time window, as (time, record, slot_tx), in time order."""
        lo = -float("inf") if t0 is None else t0
        hi = float("inf") if t1 is None else t1
        with self._connect() as conn:
            return conn.execute(
                "SELECT t, record, slot_tx FROM grid_slots WHERE t >= ? AND t <= ? AND record % ? = 0 ORDER BY t", (lo, hi, every)
            ).fetchall()

    def grid_cells(self, level: int, t0: float | None, t1: float | None) -> list[tuple[float, float, int, str, float]]:
        """The resource grid occupancy of bins of level slots overlapping a time window, as (start, end, CRB group,
        channel, occupancy).
        """
        lo = -float("inf") if t0 is None else t0
        hi = float("inf") if t1 is None else t1
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT t0, t1, cells FROM grid_cells WHERE level = ? AND t0 <= ? AND t1 >= ? ORDER BY t0", (level, hi, lo)
            ).fetchall()
        return [(start, end, grp, channel, occ) for start, end, cells in rows for grp, channel, occ in json.loads(cells)]

    def lane_links(self) -> dict[int, dict[str, Any]]:
        """The values that link each UE lane to the lanes of other sources, by lane, see parsers.correlate.ues.Context."""
        if self._events_uri is None:
            return {}
        with closing(sqlite3.connect(self._events_uri, uri=True, check_same_thread=False)) as conn:
            rows = conn.execute("SELECT lane, links FROM lane_info WHERE links IS NOT NULL").fetchall()
        return {lane: json.loads(links) for lane, links in rows}

    def context_values(self, dataset: str, field: str, limit: int = 1000) -> list[Any]:
        """Returns the distinct values of a field."""
        ds = self._dataset(dataset)
        if field not in ds["fields"]:
            raise QueryError(f"Unknown field {field!r} of {dataset!r}.")
        col = _quote(field)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT DISTINCT {col} FROM {_quote('ds_' + dataset)} WHERE {col} IS NOT NULL ORDER BY {col} LIMIT ?",
                (limit,),
            ).fetchall()
        return [r[0] for r in rows]

    def records(self, around: int | None = None, count: int = 50, after: int | None = None,
                before: int | None = None) -> list[dict[str, Any]]:
        """Returns raw records in order: centred on the record around, or the count records after the record after, or
        before the record before. Texts are the ones set by the source, else the lines of its file.
        """
        with self._connect() as conn:
            if self._sparse:
                return self._sparse_records(conn, around, count, after, before)
            if conn.execute("SELECT 1 FROM record_texts LIMIT 1").fetchone():
                if after is not None:
                    rows = conn.execute("SELECT record, text FROM record_texts WHERE record > ? ORDER BY record LIMIT ?",
                                        (after, count)).fetchall()
                elif before is not None:
                    rows = conn.execute("SELECT record, text FROM record_texts WHERE record < ? ORDER BY record DESC LIMIT ?",
                                        (before, count)).fetchall()[::-1]
                else:
                    rows = conn.execute("SELECT record, text FROM record_texts WHERE record >= ? ORDER BY record LIMIT ?",
                                        (max(1, around - count // 2), count)).fetchall()
                return [{"record": r, "text": text} for r, text in rows]
            if after is not None:
                first = after + 1
            elif before is not None:
                first, count = max(1, before - count), min(count, before - 1)
            else:
                first = max(1, around - count // 2)
            if count <= 0:
                return []
            row = conn.execute(
                "SELECT record, offset FROM record_offsets WHERE record <= ? ORDER BY record DESC LIMIT 1", (first,)
            ).fetchone()
        record, offset = row if row else (1, 0)
        out = []
        with self.path.open("rb") as f:
            f.seek(offset)
            for raw in f:
                if record >= first:
                    out.append({"record": record, "text": raw.decode("utf-8", "replace").rstrip("\r\n")})
                    if len(out) == count:
                        break
                record += 1
        return out

    def _sparse_records(self, conn: sqlite3.Connection, around: int | None, count: int, after: int | None,
                        before: int | None) -> list[dict[str, Any]]:
        """The records of a store whose records are some lines of its file, read at their offsets."""
        if after is not None:
            rows = conn.execute("SELECT record, offset FROM record_lines WHERE record > ? ORDER BY record LIMIT ?",
                                (after, count)).fetchall()
        elif before is not None:
            rows = conn.execute("SELECT record, offset FROM record_lines WHERE record < ? ORDER BY record DESC LIMIT ?",
                                (before, count)).fetchall()[::-1]
        else:
            rows = conn.execute("SELECT record, offset FROM record_lines WHERE record < ? ORDER BY record DESC LIMIT ?",
                                (around, count // 2)).fetchall()[::-1]
            rows += conn.execute("SELECT record, offset FROM record_lines WHERE record >= ? ORDER BY record LIMIT ?",
                                 (around, count - len(rows))).fetchall()
        out = []
        with self.path.open("rb") as f:
            for record, offset in rows:
                f.seek(offset)
                out.append({"record": record, "text": f.readline().decode("utf-8", "replace").rstrip("\r\n")})
        return out

    def search(self, pattern: str, regex: bool = False, after: int = 0, backward: bool = False) -> dict[str, Any]:
        """Returns the first record after the record after, or with backward the last before it, whose text matches
        pattern, as "record", or None. The search wraps around the end of the records, which "wrapped" tells.

        pattern is a case-insensitive regular expression with regex, else plain text. Raises QueryError for an invalid
        expression.
        """
        try:
            compiled = re.compile(pattern if regex else re.escape(pattern), re.IGNORECASE | re.MULTILINE)
            # Logs are searched as bytes, where some valid expressions are not, e.g. with \u escapes.
            compiled_bytes = re.compile(compiled.pattern.encode(), compiled.flags & ~re.UNICODE)
        except re.error as e:
            raise QueryError(f"Search: {e}") from None
        first = self._search(compiled, compiled_bytes, after, backward)
        if first is not None:
            return {"record": first, "wrapped": False}
        # From the other end, up to where the search started, that record included.
        again = self._search(compiled, compiled_bytes, 2**62 if backward else 0, backward,
                             after - 1 if backward else after + 1)
        return {"record": again, "wrapped": again is not None}

    def _search(
        self, compiled: re.Pattern, compiled_bytes: re.Pattern, after: int, backward: bool, stop: int | None = None
    ) -> int | None:
        """The first record after after, or the last before it with backward, matching compiled, or compiled_bytes
        in a log read from its file, short of stop.
        """
        def wanted(record: int) -> bool:
            if backward:
                return record < after and (stop is None or record > stop)
            return record > after and (stop is None or record < stop)

        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM record_texts LIMIT 1").fetchone():
                op, order = ("<", "DESC") if backward else (">", "ASC")
                rows = conn.execute(f"SELECT record, text FROM record_texts WHERE record {op} ? ORDER BY record {order}", (after,))
                return next((r for r, text in rows if wanted(r) and compiled.search(text)), None)
            index = conn.execute("SELECT record, offset FROM record_offsets ORDER BY record").fetchall() or [(1, 0)]
        blocks = _search_blocks(index, self.path.stat().st_size)
        # Logs are searched as bytes, one block of lines at a time, the line of a match from the newlines before it.
        with self.path.open("rb") as f:
            for first, last, start, end in reversed(blocks) if backward else blocks:
                if (backward and first >= after) or (not backward and last <= after):
                    continue
                f.seek(start)
                data = f.read(end - start)
                # So that $ matches at the end of CRLF lines too. Newlines, which give the line numbers, stay.
                if b"\r" in data:
                    data = data.replace(b"\r\n", b"\n")
                found, line, pos = None, first, 0
                for m in compiled_bytes.finditer(data):
                    line += data.count(b"\n", pos, m.start())
                    pos = m.start()
                    if wanted(line):
                        if not backward:
                            return line
                        found = line
                    elif backward and line >= after:
                        break
                if found is not None:
                    return found
                if stop is not None and ((backward and first <= stop) or (not backward and last >= stop)):
                    return None
        return None

    def _window(
        self,
        conn: sqlite3.Connection,
        ds: dict[str, Any],
        field: str | None,
        t0: float | None,
        t1: float | None,
        split_by: str | None,
        split_values: list[str] | None,
        filter_expr: str | None,
        instance: str | None,
        max_splits: int,
    ) -> tuple[str, list[Any], int]:
        """Builds the WHERE clause selecting rows, returning it with its params and split count.

        With a field, only rows where that numeric field has a value are selected.
        """
        if field is not None and ds["fields"].get(field) != "number":
            raise QueryError(f"Field {field!r} of {ds['name']!r} is not numeric.")
        if split_by is not None and split_by not in ds["fields"]:
            raise QueryError(f"Unknown split field {split_by!r} of {ds['name']!r}.")

        table = _quote("ds_" + ds["name"])
        split_col = _quote(split_by) if split_by else "NULL"
        where = [f"{_quote(field)} IS NOT NULL"] if field is not None else ["1"]
        params: list[Any] = []
        if t0 is not None:
            where.append(f"{_TS} >= ?")
            params.append(t0)
        if t1 is not None:
            where.append(f"{_TS} <= ?")
            params.append(t1)
        if filter_expr and filter_expr.strip():
            try:
                cond, cond_params = compile_filter(filter_expr, ds["fields"], _quote)
            except FilterError as e:
                raise QueryError(f"Filter: {e}") from None
            where.append(cond)
            params.extend(cond_params)
        if instance is not None:
            if not ds["instance"]:
                raise QueryError(f"Dataset {ds['name']!r} has no instance field.")
            where.append(f"CAST({_quote(ds['instance'])} AS TEXT) = ?")
            params.append(instance)
        if split_by and split_values:
            where.append(f"CAST({split_col} AS TEXT) IN ({', '.join('?' * len(split_values))})")
            params.extend(split_values)

        total_splits = 1
        if split_by:
            where_sql = " AND ".join(where)
            total_splits = conn.execute(f"SELECT COUNT(DISTINCT {split_col}) FROM {table} WHERE {where_sql}", params).fetchone()[0]
            if not split_values and total_splits > max_splits:
                first = conn.execute(
                    f"SELECT DISTINCT {split_col} FROM {table} WHERE {where_sql} ORDER BY {split_col} LIMIT ?",
                    params + [max_splits],
                ).fetchall()
                where.append(f"{split_col} IN ({', '.join('?' * len(first))})")
                params.extend(r[0] for r in first)
        return " AND ".join(where), params, total_splits

    @staticmethod
    def _table_columns(ds: dict[str, Any], fields: list[str] | None) -> list[str]:
        if fields:
            unknown = [f for f in fields if f not in ds["fields"]]
            if unknown:
                raise QueryError(f"Unknown fields {unknown} of {ds['name']!r}.")
            return list(fields)
        context = [c for c in ds["context"] if c in ds["fields"]]
        return context + [f for f in ds["fields"] if f not in context]

    def _dataset(self, dataset: str) -> dict[str, Any]:
        try:
            return self.datasets[dataset]
        except KeyError:
            raise QueryError(f"Unknown dataset {dataset!r}.") from None

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self._uri, uri=True, check_same_thread=False)) as conn:
            yield conn


def _label(field: str, split_by: str | None, value: Any) -> str:
    return f"{split_by}={value}" if split_by else field


def _percentiles(values: list[float], percents: tuple[int, ...]) -> list[float | None]:
    """Linear interpolation percentiles, None for an empty list."""
    if not values:
        return [None] * len(percents)
    values = sorted(values)
    out = []
    for p in percents:
        pos = (len(values) - 1) * p / 100
        lo = int(pos)
        hi = min(lo + 1, len(values) - 1)
        out.append(values[lo] + (values[hi] - values[lo]) * (pos - lo))
    return out


def _split_sort_key(value: Any) -> tuple:
    if value is None:
        return (2, "")
    if isinstance(value, (int, float)):
        return (0, value)
    return (1, str(value))


def _file_uri(path: Path) -> str:
    return f"file:{urllib.parse.quote(str(path))}?mode=ro"


def _build(conn: sqlite3.Connection, path: Path, source_type: SourceType, progress: ProgressFn | None) -> None:
    conn.execute("PRAGMA journal_mode = OFF")
    conn.execute("PRAGMA synchronous = OFF")
    writer = StoreWriter(conn)
    source_type.parse(path, writer, progress)
    writer.finish(path, source_type.name)


def _build_events(conn: sqlite3.Connection, path: Path, source_type: SourceType) -> None:
    conn.execute("PRAGMA journal_mode = OFF")
    conn.execute("PRAGMA synchronous = OFF")
    writer = EventStoreWriter(conn)
    source_type.parse_events(path, writer)
    writer.finish()


def _memory_uri() -> str:
    return f"file:ocudu-viz-{os.getpid()}-{next(_memory_db_ids)}?mode=memory&cache=shared"


def default_cache_dir() -> Path:
    """Per-user directory under the system temp dir."""
    return Path(tempfile.gettempdir()) / f"ocudu-viz-{os.getuid()}"


class StoreCache:
    """Opens sources through a directory of cached databases, evicting the least recently used ones."""

    def __init__(self, cache_dir: Path | None = None, max_bytes: int = 2 << 30, enabled: bool = True):
        self.cache_dir = cache_dir or default_cache_dir()
        self.max_bytes = max_bytes
        self.enabled = enabled

    def open(self, path: Path, source_type: SourceType, progress: ProgressFn | None = None) -> Store:
        """Returns the store of a source, parsing it if there is no valid cache."""
        path = path.resolve()
        if not self.enabled:
            uri = _memory_uri()
            keepalive = sqlite3.connect(uri, uri=True, check_same_thread=False)
            _build(keepalive, path, source_type, progress)
            return Store(uri, keepalive)

        self._make_dir()
        db = self.cache_dir / (self._key(path, source_type) + _CACHE_SUFFIX)
        self._build_file(db, lambda conn: _build(conn, path, source_type, progress))
        self.evict(keep={db})
        return Store(_file_uri(db))

    def is_cached(self, path: Path, source_type: SourceType) -> bool:
        """Whether a source has a valid cache, so that opening it takes no parsing."""
        if not self.enabled:
            return False
        path = path.resolve()
        return self._is_complete(self.cache_dir / (self._key(path, source_type) + _CACHE_SUFFIX))

    def open_events(self, store: Store, source_type: SourceType) -> None:
        """Serves the events of a source in its store, parsing them if there is no valid cache.

        Source types without events give no events.
        """
        if not hasattr(source_type, "parse_events"):
            store.set_events(None)
            return
        path = store.path
        if not self.enabled:
            uri = _memory_uri()
            keepalive = sqlite3.connect(uri, uri=True, check_same_thread=False)
            _build_events(keepalive, path, source_type)
            store.set_events(uri, keepalive)
            return

        self._make_dir()
        key = self._key(path, source_type)
        db = self.cache_dir / (key + _EVENTS_SUFFIX)
        self._build_file(db, lambda conn: _build_events(conn, path, source_type))
        self.evict(keep={db, self.cache_dir / (key + _CACHE_SUFFIX)})
        store.set_events(_file_uri(db))

    def evict(self, keep: Iterable[Path] = ()) -> None:
        """Removes the least recently used caches until the cache dir fits in max_bytes."""
        keep = set(keep)
        files = sorted(self.cache_dir.glob("*" + _CACHE_SUFFIX), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        for p in files:
            if total <= self.max_bytes:
                break
            if p in keep:
                continue
            total -= p.stat().st_size
            p.unlink(missing_ok=True)

    def clear(self) -> None:
        """Removes all cached databases."""
        if self.cache_dir.is_dir():
            for p in self.cache_dir.glob("*" + _CACHE_SUFFIX):
                p.unlink(missing_ok=True)

    def _build_file(self, db: Path, build: Callable[[sqlite3.Connection], None]) -> None:
        """Builds a cache file, unless it is complete already, and marks its use."""
        if not self._is_complete(db):
            # Built aside and renamed, so that readers never see a partial database.
            # Unique per build: servers in separate containers share the cache directory and may share a PID.
            tmp = db.with_suffix(f".{uuid.uuid4().hex}.tmp")
            try:
                conn = sqlite3.connect(tmp)
                try:
                    build(conn)
                finally:
                    conn.close()
                os.replace(tmp, db)
            finally:
                tmp.unlink(missing_ok=True)
        # The mtime of a cache file marks its last use, for eviction.
        os.utime(db)

    def _make_dir(self) -> None:
        self.cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    @staticmethod
    def _key(path: Path, source_type: SourceType) -> str:
        st = path.stat()
        ident = [str(path), st.st_size, st.st_mtime_ns, source_type.name, source_type.version, SCHEMA_VERSION]
        return hashlib.sha256(json.dumps(ident).encode()).hexdigest()[:32]

    @staticmethod
    def _is_complete(db: Path) -> bool:
        if not db.is_file():
            return False
        try:
            with closing(sqlite3.connect(_file_uri(db), uri=True)) as conn:
                row = conn.execute("SELECT value FROM meta WHERE key = 'complete'").fetchone()
        except sqlite3.Error:
            return False
        return bool(row and json.loads(row[0]))


# Bytes a log search reads at once, joined from the record offsets of the store.
_SEARCH_BLOCK_SIZE = 8 << 20


def _search_blocks(index: list[tuple[int, int]], size: int) -> list[tuple[int, int, int, int]]:
    """Blocks of a file to search, as (first record, last record, start offset, end offset), at the record offsets of
    index. The last block ends at the end of the file, with no last record known.
    """
    blocks = []
    record, start = index[0]
    for next_record, offset in index[1:]:
        if offset - start >= _SEARCH_BLOCK_SIZE:
            blocks.append((record, next_record - 1, start, offset))
            record, start = next_record, offset
    blocks.append((record, 2**62, start, size))
    return blocks

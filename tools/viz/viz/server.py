# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""HTTP API over the source stores, and the static frontend."""

from __future__ import annotations

import csv
import heapq
import io
import itertools
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from parsers.log import adhoc
from pydantic import BaseModel
from starlette.types import Scope

from .files import PathNotAllowed, list_dir
from .registry import OpenError, RunEntry, SourceEntry, SourceRegistry
from .filters import FilterError
from .store import QueryError, Store
from .trace import RunTrace, filter_trace, group_trace

# Built frontend, produced by "npm run build" in the frontend directory.
STATIC_DIR = Path(__file__).parent / "static"
_NOT_BUILT_PAGE = (
    "<!doctype html><title>ocudu-viz</title><p>The ocudu-viz frontend is not built. Run the ocudu-viz "
    "wrapper script, which builds it in the container image, or run <code>npm ci && npm run build</code> in "
    "<code>tools/viz/frontend</code>.</p>"
)
# Rows per CSV chunk sent to the client.
_CSV_CHUNK_ROWS = 5000


def _utc_iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _csv_name(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in text)


def display_names(paths: list[Path]) -> list[str]:
    """File names, prefixed by the first differing directory when several sources share a name."""
    names = []
    for path in paths:
        twins = [p for p in paths if p.name == path.name and p != path]
        if not twins:
            names.append(path.name)
            continue
        parts = path.parent.parts
        idx = min(
            next((i for i, (a, b) in enumerate(zip(parts, t.parent.parts)) if a != b), min(len(parts), len(t.parent.parts)))
            for t in twins
        )
        if idx >= len(parts):
            names.append(str(path))
        elif idx == len(parts) - 1:
            names.append(f"{parts[idx]}/{path.name}")
        else:
            names.append(f"{parts[idx]}/\u2026/{path.name}")
    return names


class _RevalidatedStaticFiles(StaticFiles):
    """Static files that browsers revalidate on every load, so that frontend changes are never served stale."""

    async def get_response(self, path: str, scope: Scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _source_info(entry: SourceEntry, name: str) -> dict[str, Any]:
    info: dict[str, Any] = {
        "id": entry.id,
        "path": str(entry.path),
        "name": name,
        "file": entry.path.name,
        "size": _file_size(entry.path),
        "type": entry.source_type.name if entry.source_type else None,
        "has_detail": hasattr(entry.source_type, "record_detail"),
        "status": entry.status,
        "progress": entry.progress,
        "error": entry.error,
        "t_min": None,
        "t_max": None,
        "datasets": [],
        "events_status": entry.events_status,
        "notes": [],
        "event_counts": {},
        "event_type_counts": {},
        "parent": entry.parent,
    }
    if (spec := getattr(entry.source_type, "spec", None)) is not None:
        info["adhoc"] = {"key": spec.key, "filter": spec.filter, "split_key": spec.split_key}
        info["name"] = f"{name} › {entry.source_type.label}"
    s = entry.store
    if s is None:
        return info
    info["t_min"], info["t_max"] = s.meta.get("t_min"), s.meta.get("t_max")
    info["event_counts"] = s.event_counts
    info["event_type_counts"] = s.event_type_counts
    info["notes"] = s.meta.get("notes") or []
    if grid := s.meta.get("grid"):
        info["grid"] = {k: v for k, v in grid.items() if k not in ("pucch_resources", "coresets", "search_spaces", "prach")}
    info["datasets"] = [
        {
            "name": ds["name"],
            "kind": ds["kind"],
            "fields": [{"name": f, "type": t, "unit": ds["units"].get(f)} for f, t in ds["fields"].items()],
            "context": ds["context"],
            "label": ds["label"],
            "instance": ds["instance"],
            "t_min": ds["t_min"],
            "t_max": ds["t_max"],
        }
        for ds in s.datasets.values()
    ]
    return info


def _run_info(run: RunEntry, name: str) -> dict[str, Any]:
    return {
        "id": run.id,
        "path": str(run.path),
        "name": name,
        "kind": "dir" if run.is_dir else "file",
        "sources": run.source_ids,
    }


class _OpenRequest(BaseModel):
    path: str


class _AdhocRequest(BaseModel):
    key: str | None = None
    filter: str | None = None
    split_key: str | None = None


def create_app(
    sources: SourceRegistry | list[Store], static_dir: Path = STATIC_DIR, on_shutdown: Callable[[], None] | None = None
) -> FastAPI:
    """Serves the sources of a registry, or a fixed list of stores, and the frontend in static_dir.

    Each store of a list is a run of its own. Run and source ids are their positions in opening order. on_shutdown runs
    when the server stops.
    """
    if isinstance(sources, SourceRegistry):
        registry = sources
    else:
        registry = SourceRegistry()
        for store in sources:
            registry.add_run(store.path, False, [registry.add_store(store).id])

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        if on_shutdown is not None:
            on_shutdown()

    app = FastAPI(title="ocudu-viz", lifespan=lifespan)

    def get_store(source: int) -> Store:
        try:
            store = registry.get_ready(source)
        except KeyError:
            raise HTTPException(404, f"Unknown source {source}.") from None
        if store is None:
            raise HTTPException(409, f"Source {source} is not parsed yet.")
        return store

    def sources_info() -> list[dict[str, Any]]:
        entries = registry.entries()
        names = display_names([e.path for e in entries])
        return [_source_info(e, names[i]) for i, e in enumerate(entries)]

    def runs_info() -> list[dict[str, Any]]:
        runs = [r for r in registry.runs() if r.status == "open"]
        names = display_names([r.path for r in runs])
        return [_run_info(r, names[i] + ("/" if r.is_dir else "")) for i, r in enumerate(runs)]

    def run_info(run_id: int) -> dict[str, Any]:
        info = next((r for r in runs_info() if r["id"] == run_id), None)
        if info is None:
            raise HTTPException(404, f"Unknown run {run_id}.")
        return info

    @app.get("/api/sources")
    def list_sources() -> list[dict[str, Any]]:
        return sources_info()

    @app.post("/api/sources/{source_id}/parse")
    def parse_source(source_id: int) -> dict[str, Any]:
        """Parses a source left to parse on request, e.g. the MAC pcap of a run."""
        try:
            registry.parse(source_id)
        except KeyError:
            raise HTTPException(404, f"Unknown source {source_id}.") from None
        return {"id": source_id}

    @app.post("/api/sources/{source_id}/adhoc")
    def open_adhoc(source_id: int, req: _AdhocRequest) -> dict[str, Any]:
        """Opens the ad-hoc metric of a key=value field of the lines of a log, or of the log of an ad-hoc metric, or
        without a key, the lines that match a filter, as events.
        """
        try:
            spec = adhoc.Spec(req.key, req.filter or None, req.split_key or None)
            entry = registry.open_adhoc(source_id, spec)
        except KeyError:
            raise HTTPException(404, f"Unknown source {source_id}.") from None
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return {"id": entry.id}

    @app.get("/api/runs")
    def list_runs() -> list[dict[str, Any]]:
        """The open runs, each with the ids of its sources."""
        return runs_info()

    @app.post("/api/runs")
    def open_run(req: _OpenRequest) -> dict[str, Any]:
        """Opens a directory as a run of its supported files, or a file as a run of that file."""
        try:
            run = registry.open_run(req.path)
        except (OpenError, PathNotAllowed) as e:
            raise HTTPException(400, str(e)) from None
        return run_info(run.id)

    @app.delete("/api/runs/{run_id}")
    def close_run(run_id: int) -> dict[str, Any]:
        try:
            registry.close_run(run_id)
        except KeyError:
            raise HTTPException(404, f"Unknown run {run_id}.") from None
        return {"id": run_id}

    @app.get("/api/runs/{run_id}/files")
    def run_files(run_id: int) -> list[dict[str, Any]]:
        """The supported files of the directory of a run, with whether the run has them."""
        info = run_info(run_id)
        run = registry.runs()[run_id]
        open_paths = {e.path for e in registry.entries() if e.id in info["sources"]}
        try:
            files = registry.supported_files(run.directory)
        except OpenError as e:
            raise HTTPException(400, str(e)) from None
        return [{"name": f.name, "path": str(f), "in_run": f.resolve() in open_paths} for f in files]

    @app.get("/api/runs/{run_id}/related")
    def related_files(run_id: int) -> list[dict[str, Any]]:
        """The files of the directory of a run, not in it, written by the same run as its files."""
        try:
            files = registry.related_files(run_id)
        except KeyError:
            raise HTTPException(404, f"Unknown run {run_id}.") from None
        except OpenError as e:
            raise HTTPException(400, str(e)) from None
        return [{"name": f.name, "path": str(f)} for f in files]

    @app.post("/api/runs/{run_id}/promote")
    def promote_run(run_id: int) -> dict[str, Any]:
        """Adds the related files of a run, making it the run of its directory, or returns the open run of its directory."""
        try:
            run = registry.promote_run(run_id)
        except KeyError:
            raise HTTPException(404, f"Unknown run {run_id}.") from None
        except (OpenError, PathNotAllowed) as e:
            raise HTTPException(400, str(e)) from None
        return run_info(run.id)

    @app.post("/api/runs/{run_id}/sources")
    def add_to_run(run_id: int, req: _OpenRequest) -> dict[str, Any]:
        try:
            registry.add_to_run(run_id, req.path)
        except KeyError:
            raise HTTPException(404, f"Unknown run {run_id}.") from None
        except (OpenError, PathNotAllowed) as e:
            raise HTTPException(400, str(e)) from None
        return run_info(run_id)

    @app.delete("/api/runs/{run_id}/sources/{source_id}")
    def remove_from_run(run_id: int, source_id: int) -> dict[str, Any]:
        try:
            run = registry.remove_from_run(run_id, source_id)
        except KeyError:
            raise HTTPException(404, f"Unknown run {run_id}.") from None
        return run_info(run_id) if run.status == "open" else {"id": run_id, "sources": []}

    @app.get("/api/roots")
    def roots() -> dict[str, Any]:
        return {"roots": [str(r) for r in registry.roots], "can_open": registry.cache is not None}

    @app.get("/api/fs")
    def fs(path: str | None = None, hidden: bool = False) -> dict[str, Any]:
        if not registry.roots:
            raise HTTPException(400, "No directories are available to browse.")
        try:
            return list_dir(path or registry.roots[0], registry.roots, hidden)
        except PathNotAllowed as e:
            raise HTTPException(400, str(e)) from None

    @app.get("/api/series")
    def series(
        source: int,
        dataset: str,
        field: str,
        t0: float | None = None,
        t1: float | None = None,
        width: int = Query(1000, ge=1, le=20000),
        split_by: str | None = None,
        split_values: list[str] | None = Query(None),
        filter_expr: str | None = Query(None, alias="filter"),
        instance: str | None = None,
    ) -> dict[str, Any]:
        try:
            return get_store(source).series(dataset, field, t0, t1, width, split_by, split_values, filter_expr, instance)
        except QueryError as e:
            raise HTTPException(400, str(e)) from None

    @app.get("/api/stats")
    def stats(
        source: int,
        dataset: str,
        field: str,
        t0: float | None = None,
        t1: float | None = None,
        split_by: str | None = None,
        split_values: list[str] | None = Query(None),
        filter_expr: str | None = Query(None, alias="filter"),
        instance: str | None = None,
    ) -> dict[str, Any]:
        try:
            return get_store(source).stats(dataset, field, t0, t1, split_by, split_values, filter_expr, instance)
        except QueryError as e:
            raise HTTPException(400, str(e)) from None

    @app.get("/api/histogram")
    def histogram(
        source: int,
        dataset: str,
        field: str,
        t0: float | None = None,
        t1: float | None = None,
        split_by: str | None = None,
        split_values: list[str] | None = Query(None),
        filter_expr: str | None = Query(None, alias="filter"),
        instance: str | None = None,
        bins: int = Query(50, ge=1, le=1000),
    ) -> dict[str, Any]:
        try:
            return get_store(source).histogram(dataset, field, t0, t1, split_by, split_values, filter_expr, instance, bins)
        except QueryError as e:
            raise HTTPException(400, str(e)) from None

    @app.get("/api/table")
    def table(
        source: int,
        dataset: str,
        t0: float | None = None,
        t1: float | None = None,
        filter_expr: str | None = Query(None, alias="filter"),
        instance: str | None = None,
        limit: int = Query(1000, ge=1, le=100_000),
    ) -> dict[str, Any]:
        store = get_store(source)
        try:
            columns = store.table_columns(dataset)
            total = store.count_table_rows(dataset, t0, t1, filter_expr, instance)
            rows = [list(r) for r in store.table_rows(dataset, t0, t1, filter_expr, instance, columns, limit)]
        except QueryError as e:
            raise HTTPException(400, str(e)) from None
        ds = store.datasets[dataset]
        fields = [{"name": c, "type": ds["fields"][c], "unit": ds["units"].get(c), "context": c in ds["context"]} for c in columns]
        return {"fields": fields, "total": total, "rows": rows}

    @app.get("/api/table.csv")
    def table_csv(
        source: int,
        dataset: str,
        t0: float | None = None,
        t1: float | None = None,
        filter_expr: str | None = Query(None, alias="filter"),
        instance: str | None = None,
        fields: list[str] | None = Query(None),
    ) -> StreamingResponse:
        store = get_store(source)
        try:
            columns = store.table_columns(dataset, fields)
            # Validates the query before the response starts, so errors get a proper status.
            store.count_table_rows(dataset, t0, t1, filter_expr, instance)
        except QueryError as e:
            raise HTTPException(400, str(e)) from None
        units = store.datasets[dataset]["units"]
        # Header names carry the unit, with "%" spelled out to stay a plain identifier.
        header = ["time_utc", "line"] + [f"{c}_{units[c].replace('%', 'pct')}" if c in units else c for c in columns]

        def generate() -> Iterator[str]:
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(header)
            rows = store.table_rows(dataset, t0, t1, filter_expr, instance, columns, limit=None)
            for i, (t, rec, *values) in enumerate(rows, start=1):
                writer.writerow([_utc_iso(t), rec, *values])
                if i % _CSV_CHUNK_ROWS == 0:
                    yield buf.getvalue()
                    buf.seek(0)
                    buf.truncate()
            yield buf.getvalue()

        name_parts = [store.path.stem, dataset] + ([instance] if instance else [])
        filename = _csv_name("_".join(name_parts)) + ".csv"
        return StreamingResponse(
            generate(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )

    @app.get("/api/series.csv")
    def series_csv(
        source: int,
        dataset: str,
        field: str,
        t0: float | None = None,
        t1: float | None = None,
        split_by: str | None = None,
        split_values: list[str] | None = Query(None),
        filter_expr: str | None = Query(None, alias="filter"),
        instance: str | None = None,
        field2: str | None = None,
    ) -> StreamingResponse:
        """Streams the samples of the series of a plot at full resolution, one row per sample. With field2, the second
        metric of the plot, the samples of both, in time order, with their field and unit in columns.
        """
        store = get_store(source)
        fields = [field, *([field2] if field2 else [])]
        units = store.datasets[dataset]["units"] if dataset in store.datasets else {}
        def samples(f: str) -> Iterator[tuple[Any, ...]]:
            for t, _, split, value in store.series_rows(dataset, f, t0, t1, split_by, split_values, filter_expr, instance):
                yield t, f, split, value

        streams = [samples(f) for f in fields]
        rows = heapq.merge(*streams, key=lambda r: r[0]) if field2 else streams[0]
        try:
            # Runs the query before the response starts, so errors get a proper status.
            first = next(rows, None)
        except QueryError as e:
            raise HTTPException(400, str(e)) from None
        unit = units.get(field)
        split_col = [split_by] if split_by else []
        if field2:
            header = ["time_utc", *split_col, "field", "value", "unit"]
        else:
            header = ["time_utc", *split_col, f"{field}_{unit.replace('%', 'pct')}" if unit else field]

        def generate() -> Iterator[str]:
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(header)
            for i, (t, f, split, value) in enumerate(itertools.chain([first] if first else [], rows), start=1):
                extra = [f, value, units.get(f) or ""] if field2 else [value]
                writer.writerow([_utc_iso(t), *([split] if split_by else []), *extra])
                if i % _CSV_CHUNK_ROWS == 0:
                    yield buf.getvalue()
                    buf.seek(0)
                    buf.truncate()
            yield buf.getvalue()

        name_parts = [store.path.stem, dataset] + ([instance] if instance else []) + fields
        filename = _csv_name("_".join(name_parts)) + ".csv"
        return StreamingResponse(
            generate(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )

    @app.get("/api/events")
    def events(
        source: int,
        t0: float | None = None,
        t1: float | None = None,
        categories: list[str] | None = Query(None),
        ue: int | None = None,
        limit: int = Query(5000, ge=1, le=50_000),
        types: list[str] | None = Query(None),
    ) -> dict[str, Any]:
        """Returns the events of a time window, of the given categories or types, all without either."""
        return get_store(source).events(t0, t1, categories, ue, limit, types)

    # Joined trace of each run, with the source states it was built from.
    run_traces: dict[int, tuple[tuple, RunTrace]] = {}

    @app.get("/api/runs/{run_id}/trace")
    def run_trace(
        run_id: int,
        t0: float | None = None,
        t1: float | None = None,
        max_lanes: int = Query(300, ge=1, le=5000),
        limit: int = Query(5000, ge=1, le=50_000),
        sources: list[int] | None = Query(None),
        filter_expr: str | None = Query(None, alias="filter"),
        group_by: str | None = None,
    ) -> dict[str, Any]:
        """Returns the trace of a run: the UE contexts of its F1AP pcap with the UE events of its logs joined to them,
        like /api/trace. Each event has its source. With sources, only their events and the lanes they have events in.
        """
        info = run_info(run_id)
        entries = [e for e in registry.entries() if e.id in info["sources"] and e.store is not None and e.store.events_ready]
        anchor = next((e for e in entries if e.source_type and e.source_type.name == "pcap" and "f1ap" in e.store.event_counts), None)
        if anchor is None:
            raise HTTPException(400, f"Run {run_id} has no parsed F1AP pcap.")
        logs = [e for e in entries if e.source_type and e.source_type.name != "pcap"]
        # NGAP and E1AP pcaps, which follow the UEs through their F1AP contexts, see RunTrace.
        cores = [e for e in entries if e is not anchor and {"ngap", "e1ap"} & set(e.store.event_counts)]
        key = (anchor.id, tuple(e.id for e in logs), tuple(e.id for e in cores))
        cached = run_traces.get(run_id)
        if cached is None or cached[0] != key:
            run_trace = RunTrace((anchor.id, anchor.store), [(e.id, e.store) for e in logs], [(e.id, e.store) for e in cores])
            cached = run_traces[run_id] = (key, run_trace)
        try:
            return cached[1].trace(t0, t1, max_lanes, limit, set(sources) if sources else None, filter_expr, group_by)
        except FilterError as e:
            raise HTTPException(400, str(e)) from None

    @app.get("/api/trace")
    def trace(
        source: int,
        t0: float | None = None,
        t1: float | None = None,
        max_lanes: int = Query(300, ge=1, le=5000),
        limit: int = Query(5000, ge=1, le=50_000),
        filter_expr: str | None = Query(None, alias="filter"),
        group_by: str | None = None,
    ) -> dict[str, Any]:
        """Returns the UE lanes and events of a time window, of the events matching filter if given, see filter_trace(),
        with the lanes of the same group_by identifier merged if given, see group_trace().
        """
        store = get_store(source)
        if not filter_expr and not group_by:
            return store.trace(t0, t1, max_lanes, limit)
        try:
            trace = store.trace(t0, t1, 10**9, 10**9)
            if not group_by:
                return filter_trace(trace, filter_expr, max_lanes, limit)
            if filter_expr:
                trace = filter_trace(trace, filter_expr, 10**9, 10**9)
            return group_trace(trace, group_by, max_lanes, limit)
        except FilterError as e:
            raise HTTPException(400, str(e)) from None

    @app.get("/api/context")
    def context(source: int, dataset: str, field: str) -> list[Any]:
        try:
            return get_store(source).context_values(dataset, field)
        except QueryError as e:
            raise HTTPException(400, str(e)) from None

    @app.get("/api/records")
    def records(
        source: int,
        around: int | None = Query(None, ge=1),
        after: int | None = Query(None, ge=0),
        before: int | None = Query(None, ge=1),
        count: int = Query(50, ge=1, le=1000),
        mark: int | None = None,
        field: str | None = None,
    ) -> list[dict[str, Any]]:
        """Returns the records around a record, or the count records after or before one. With mark and field, the
        record mark gets the spans of the field in its text, as "marks".
        """
        if (around, after, before).count(None) != 2:
            raise HTTPException(400, "Give one of around, after and before.")
        out = get_store(source).records(around, count, after, before)
        spans_fn = getattr(registry.source_type(source), "field_spans", None)
        if mark is not None and field and spans_fn is not None:
            for r in out:
                if r["record"] == mark and (span := spans_fn(r["text"]).get(field)) is not None:
                    r["marks"] = [list(span)]
        return out

    @app.get("/api/grid")
    def resource_grid(
        source: int,
        t0: float | None = None,
        t1: float | None = None,
        width: int = Query(1000, ge=1, le=10_000),
    ) -> dict[str, Any]:
        """Returns the resource grid of a scheduler event trace in a time window, for a chart width in pixels: its
        allocations when zoomed in, else its occupancy, see SchedtraceSource.grid().
        """
        store = get_store(source)
        grid_fn = getattr(registry.source_type(source), "grid", None)
        if grid_fn is None or "grid" not in store.meta:
            raise HTTPException(400, f"Source {source} has no resource grid.")
        return grid_fn(store.path, store, t0, t1, width)

    @app.get("/api/harq")
    def harq_transaction(
        source: int,
        record: int = Query(..., ge=1),
        item: str = Query(..., pattern=r"^\w+:\d+$"),
    ) -> dict[str, Any]:
        """Returns the HARQ transaction of an allocation of a scheduler event trace, a PDSCH, PUSCH or PDCCH given by
        its slot event and its "list:index" in the slot decision, see SchedtraceSource.harq().
        """
        store = get_store(source)
        harq_fn = getattr(registry.source_type(source), "harq", None)
        if harq_fn is None or "grid" not in store.meta:
            raise HTTPException(400, f"Source {source} has no HARQ processes.")
        name, index = item.split(":")
        try:
            out = harq_fn(store.path, store, record, (name, int(index)))
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        if out is None:
            raise HTTPException(404, f"Allocation {item} of event {record} is of no HARQ process.")
        return out

    @app.get("/api/records/search")
    def search_records(
        source: int,
        q: str = Query(..., min_length=1),
        regex: bool = False,
        after: int = Query(0, ge=0),
        backward: bool = False,
    ) -> dict[str, Any]:
        """Returns the next record after after, or the previous one with backward, whose text matches q, as "record",
        or None, wrapping around the records, see Store.search().
        """
        try:
            return get_store(source).search(q, regex, after, backward)
        except QueryError as e:
            raise HTTPException(400, str(e)) from None

    @app.get("/api/records/detail")
    def record_detail(source: int, record: int = Query(..., ge=1)) -> dict[str, Any]:
        """Returns a record decoded as text, for sources whose type decodes records, e.g. pcap frames, and as "data",
        a structure, for those that also give one, e.g. the events of a scheduler event trace.
        """
        store = get_store(source)
        source_type = registry.source_type(source)
        detail_fn = getattr(source_type, "record_detail", None)
        if detail_fn is None:
            raise HTTPException(400, f"Source {source} has no record details.")
        try:
            out: dict[str, Any] = {"record": record, "text": detail_fn(store.path, record)}
            if (data_fn := getattr(source_type, "record_data", None)) is not None:
                out["data"] = data_fn(store.path, record)
            return out
        except Exception as e:
            # A tshark failure is reported on the record rather than crashing the request.
            raise HTTPException(400, f"Could not decode record {record}: {e}") from None

    if (static_dir / "index.html").is_file():
        app.mount("/", _RevalidatedStaticFiles(directory=static_dir, html=True), name="static")
    else:

        @app.get("/", response_class=HTMLResponse)
        def not_built() -> str:
            return _NOT_BUILT_PAGE

    return app

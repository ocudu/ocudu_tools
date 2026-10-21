# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Interface between source types and the dataset store."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

# Called with (bytes_done, bytes_total) while a source is parsed.
ProgressFn = Callable[[int, int], None]


# Slack of the time spans of the files of one run, in seconds.
RUN_SLACK_S = 60.0


@dataclass(frozen=True)
class RunIdentity:
    """What tells the files of one run apart: the build that wrote a file, if known, and its time span in epochs."""

    build: tuple[str, ...] | None
    start: float
    end: float

    def same_run(self, other: RunIdentity) -> bool:
        """Whether two files come from the same run: time spans overlapping within RUN_SLACK_S, and the same build
        when both have one.
        """
        if self.build is not None and other.build is not None and self.build != other.build:
            return False
        return self.start <= other.end + RUN_SLACK_S and other.start <= self.end + RUN_SLACK_S


def column_type(value: Any) -> str | None:
    """Column type of a field value: "number", "text" or "json", or None for a missing value."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "text"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, (list, dict)):
        return "json"
    return "text"


def column_value(value: Any) -> Any:
    """Field value as stored in a column: lists and dicts as JSON text, other values as they are."""
    return json.dumps(value) if isinstance(value, (list, dict)) else value


class DatasetWriter(Protocol):
    """Sink for the datasets extracted from a source."""

    def add_row(self, dataset: str, record: int, t: float, fields: dict[str, Any]) -> None:
        """Adds one time series row. t is in seconds since the epoch, record identifies the raw record."""

    def add_rows(self, dataset: str, fields: Sequence[str], types: dict[str, set[str]], rows: list[tuple]) -> None:
        """Adds time series rows with the same fields, each a tuple of t, record and the field values in column form
        (column_value()). types holds the column types (column_type()) of the values of each field.
        """

    def set_dataset_info(
        self,
        dataset: str,
        units: dict[str, str],
        context: list[str],
        label: str | None = None,
        instance: str | None = None,
        types: dict[str, str] | None = None,
    ) -> None:
        """Sets the field units, context fields, display label and instance field of a time series dataset, and the
        column types (column_type()) of fields whose values can all be missing.

        The instance field identifies the entity each row belongs to (e.g. the executor name). A field without values
        has its declared type, else "number" with a unit, else "text".
        """

    def set_time_range(self, t_min: float, t_max: float) -> None:
        """Sets the time span of the source, which may be wider than the span of its datasets."""

    def add_record_text(self, record: int, text: str) -> None:
        """Sets the text of a record, for sources whose records are not lines of their file."""

    def add_record_offset(self, record: int, offset: int) -> None:
        """Registers the byte offset where a raw record starts. Not every record needs one."""

    def add_record_texts(self, rows: list[tuple[int, str]]) -> None:
        """Adds the texts of records, as (record, text)."""

    def add_record_lines(self, rows: list[tuple[int, int]]) -> None:
        """Makes the records some lines of the file only, as (line number, offset), rather than all of them."""

    def set_meta(self, key: str, value: Any) -> None:
        """Sets metadata of the source, e.g. the cell of a resource grid, served with its info."""

    def add_grid_slots(self, rows: list[tuple]) -> None:
        """Adds slots of a resource grid, as (record, time, file offset, size, slot_tx)."""

    def add_grid_cells(self, rows: list[tuple]) -> None:
        """Adds resource grid occupancy, as (level, bin, start, end, cells) per bin, level being its slots and cells
        the JSON list of its [CRB group, channel, occupancy].
        """

    def set_notes(self, notes: list[str]) -> None:
        """Sets notes about the source shown with it, e.g. why some data is missing."""


class EventWriter(Protocol):
    """Sink for the events extracted from a source."""

    def add_event(self, record: int, t: float, event: dict[str, Any]) -> None:
        """Adds an event with its type, category, layer, level, ue, rnti, cause, text and UE lane."""

    def add_event_rows(self, rows: list[tuple]) -> None:
        """Adds events as rows of time, record, type, category, layer, level, ue, rnti, cause, text, UE lane, count and
        span, see store.EVENT_COLUMNS.
        """

    def add_lane(self, lane: int, ue: int | None, rnti: str | None, label: str | None = None, cu_ue: int | None = None,
                 links: dict[str, Any] | None = None) -> None:
        """Sets the DU UE index, RNTI and CU-CP UE index of a UE lane, or the label shown for it instead, and the values
        that link it to the lanes of other sources, see parsers.correlate.ues.Context.
        """


class SourceType(Protocol):
    """Adapter from one artifact kind to datasets.

    A source type may also define parse_events(path, writer: EventWriter) to extract events, which runs after parse()
    while the datasets are already served, field_spans(text) to return the span of each field in the text of a record,
    by field name, run_identity(path) to return the RunIdentity of a file, which finds the other files of its run,
    record_detail(path, record) to return a decoded record as text, and on_request(path) to tell whether a file opened
    with the other files of its run is parsed only on request, e.g. a large capture.
    """

    # Unique name, part of the cache key.
    name: str
    # Bumped when the extracted datasets change, part of the cache key.
    version: str

    def accepts(self, path: Path) -> bool:
        """Returns whether this source type can read the file."""

    def parse(self, path: Path, writer: DatasetWriter, progress: ProgressFn | None = None) -> None:
        """Extracts the datasets of the file into writer."""

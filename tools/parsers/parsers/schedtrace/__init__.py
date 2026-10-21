# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Scheduler event traces of OCUDU (schedtrace): the schedtrace_cell<N>.bin files that the scheduler event tracer
writes, one per cell: a header with the version of the file format, then size-prefixed FlatBuffers CellEvent buffers.

The schemas in fbs/, which hold the file format version, are copies of those of OCUDU; copy them again and regenerate
the decoder compiled from them, _generated.py, with python -m parsers.schedtrace.codegen --ocudu <dir>.
Traces of another major version, or without a header, are rejected. Fields that traces of a newer minor version append
are ignored.
"""

from __future__ import annotations

import os
import struct
import warnings
from collections.abc import Callable, Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import _generated, schema
from .flatbuf import Decoder

FBS_DIR = Path(__file__).parent / "fbs"
# Magic bytes at the start of a trace, before its major and minor version.
MAGIC = b"SCHT"
HEADER = struct.Struct("<4sHH")
_U32 = struct.Struct("<I")
_I32 = struct.Struct("<i")
_U16_PAIR = struct.Struct("<HH")
# Largest event size, as OCUDU's trace_to_log checks it; a larger size prefix is corrupt.
MAX_EVENT_SIZE = 1 << 20
# Smallest event size, of a FlatBuffer of an empty table: its root offset, its vtable and its offset to the vtable.
MIN_EVENT_SIZE = 12
# Errors of decoding a corrupt buffer, whose offsets point anywhere.
_DECODE_ERRORS = (struct.error, IndexError, KeyError, ValueError, TypeError, OverflowError, RecursionError, MemoryError)


class FormatError(ValueError):
    """A trace without a header or of a file format version that the decoder does not support."""


class CorruptEventError(ValueError):
    """An offset of a trace without a complete event that decodes."""


# File format version of the schemas of fbs/, as (major, minor).
# Absent only from a _generated.py older than it, so that codegen, which imports this package, can still replace it.
FORMAT_VERSION: tuple[int, int] = getattr(_generated, "FORMAT_VERSION", (0, 0))


def version(data: bytes | memoryview) -> tuple[int, int] | None:
    """The file format version in the header of the contents of a trace, or None without a header."""
    if len(data) < HEADER.size:
        return None
    magic, major, minor = HEADER.unpack_from(data, 0)
    return (major, minor) if magic == MAGIC else None


def check_version(found: tuple[int, int] | None, name: str | None = None) -> None:
    """Raises FormatError unless the decoder supports a file format version of version(); warns about a newer minor.
    Messages start with name if given.
    """
    prefix = f"{name}: " if name else ""
    if found is None:
        raise FormatError(f"{prefix}not a schedtrace file of format {_text(FORMAT_VERSION)} or later, its header is "
                          "missing. Traces written before OCUDU versioned the format are not supported.")
    if found[0] != FORMAT_VERSION[0]:
        raise FormatError(f"{prefix}schedtrace format {_text(found)} is not supported, only {FORMAT_VERSION[0]}.x. "
                          "Update fbs/ of parsers.schedtrace from the OCUDU that wrote it.")
    if found[1] > FORMAT_VERSION[1]:
        warnings.warn(f"{prefix}schedtrace format {_text(found)} is newer than {_text(FORMAT_VERSION)} of the decoder, so "
                      "the fields it adds are left out.")


def first_offset(data: bytes | memoryview, name: str | None = None) -> int:
    """The offset of the first event of the contents of a trace, after its header, checked with check_version()."""
    if not _partial_header(data):
        check_version(version(data), name)
    return HEADER.size


def _partial_header(head: bytes | memoryview) -> bool:
    """Whether the start of a trace is shorter than a header but could begin one, as in a trace just opened."""
    return len(head) < HEADER.size and MAGIC.startswith(bytes(head[: len(MAGIC)]))


def _corrupt(size: int, pos: int, name: str | None = None) -> bool:
    """Whether an event size prefix is corrupt, warning about it."""
    if MIN_EVENT_SIZE <= size <= MAX_EVENT_SIZE:
        return False
    _warn_corrupt(f"event size {size}", pos, name)
    return True


def _warn_corrupt(what: str, pos: int, name: str | None) -> None:
    prefix = f"{name}: " if name else ""
    warnings.warn(f"{prefix}corrupt schedtrace {what} at offset {pos}, the events from it are left out.")


def _bad_root(buf: bytes | memoryview, base: int, size: int) -> bool:
    """Whether the root table or its vtable of the FlatBuffer of size bytes at base of buf lies outside it."""
    table = _U32.unpack_from(buf, base)[0]
    if not 4 <= table <= size - 4:
        return True
    vtable = table - _I32.unpack_from(buf, base + table)[0]
    if not 0 <= vtable <= size - 4:
        return True
    vtable_size, table_size = _U16_PAIR.unpack_from(buf, base + vtable)
    return (vtable_size < 4 or vtable_size % 2 == 1 or vtable + vtable_size > size or table_size < 4
            or table + table_size > size)


def _decode(root: Callable[[bytes | memoryview], dict[str, Any]], buf: bytes | memoryview) -> dict[str, Any] | None:
    """The event of a FlatBuffer CellEvent, or None if it is corrupt."""
    try:
        if not _bad_root(buf, 0, len(buf)):
            event = root(buf)
            value = event.pop("value") or {"type": None}
            return {**event, **value}
    except _DECODE_ERRORS:
        pass
    return None


def _text(v: tuple[int, int]) -> str:
    return f"{v[0]}.{v[1]}"


@lru_cache(maxsize=1)
def decoder() -> Decoder:
    """The decoder of CellEvent buffers, from the schemas of fbs/."""
    return Decoder(schema.load(FBS_DIR / "cell_event.fbs"))


def schema_hash() -> str:
    """Hash of the schemas and the file format version of fbs/, and of the generator and schema parser of the
    decoders.
    """
    return _generated.SCHEMA_HASH


def _root(grid_only: bool) -> Callable[[bytes | memoryview], dict[str, Any]]:
    """The decoder of CellEvent buffers of _generated.py."""
    return _generated.root_grid if grid_only else _generated.root


def events(data: bytes | memoryview, grid_only: bool = False) -> Iterator[dict[str, Any]]:
    """The events of the contents of a schedtrace file, in order.

    Each has "timestamp_us" and "type", "CellStartEvent", "CellSlotEvent" or "CellStopEvent", with the fields of that
    event, e.g. "slot_tx", "latency_us", "inputs" and "decision" for a slot. An incomplete last event, e.g. of a
    trace still being written, is left out, and so are the events from a corrupt one, with a warning: of a size prefix
    below MIN_EVENT_SIZE or over MAX_EVENT_SIZE, or whose FlatBuffer does not decode.
    grid_only keeps only the fields of codegen.GRID_FIELDS, which is faster: those that grid.allocations(), ue_metrics
    and harq read, the timestamp, slot and latency of an event, and the type of each input.
    """
    root = _root(grid_only)
    view = memoryview(data)
    pos = first_offset(view)
    while pos + 4 <= len(view):
        size = _U32.unpack_from(view, pos)[0]
        if _corrupt(size, pos) or pos + 4 + size > len(view):
            return
        event = _decode(root, view[pos + 4 : pos + 4 + size])
        if event is None:
            _warn_corrupt("event", pos, None)
            return
        yield event
        pos += 4 + size


def offsets(data: bytes | memoryview, start: int | None = None, end: int | None = None) -> list[int]:
    """The offsets of the complete events of the contents of a trace from start, which must be the offset of an event,
    or the first one, up to end, at most the end of the contents. They stop at a corrupt event, with a warning, as
    events() does, though only its size prefix and the root table of its FlatBuffer are checked, not its fields.
    """
    end = len(data) if end is None else min(end, len(data))
    out = []
    pos = first_offset(data) if start is None else start
    while pos + 4 <= end:
        size = _U32.unpack_from(data, pos)[0]
        if _corrupt(size, pos) or pos + 4 + size > end:
            break
        if _bad_root(data, pos + 4, size):
            _warn_corrupt("event", pos, None)
            break
        out.append(pos)
        pos += 4 + size
    return out


def event_at(data: bytes | memoryview, offset: int, grid_only: bool = False) -> dict[str, Any]:
    """The event at an offset of offsets(), as events() gives it. Raises CorruptEventError without a complete event that
    decodes at offset.
    """
    if not 0 <= offset <= len(data) - 4:
        raise CorruptEventError(f"no schedtrace event at offset {offset}, past the end of the contents.")
    size = _U32.unpack_from(data, offset)[0]
    if not MIN_EVENT_SIZE <= size <= MAX_EVENT_SIZE or offset + 4 + size > len(data):
        raise CorruptEventError(f"no complete schedtrace event at offset {offset}, of size {size}.")
    # A copy, since a view of an mmap left in a traceback keeps it from closing.
    event = _decode(_root(grid_only), bytes(data[offset + 4 : offset + 4 + size]))
    if event is None:
        raise CorruptEventError(f"corrupt schedtrace event at offset {offset}.")
    return event


def start_event(data: bytes | memoryview, name: str | None = None, grid_only: bool = False) -> dict[str, Any]:
    """The start event of the cell of the contents of a trace, its first event, as event_at() gives it. Raises
    FormatError, see first_offset(), or ValueError without a complete start event, e.g. of a trace just opened. Messages
    start with name if given.
    """
    pos = first_offset(data, name)
    try:
        event = event_at(data, pos, grid_only)
    except CorruptEventError:
        event = None
    if event is not None and event["type"] == "CellStartEvent":
        return event
    prefix = f"{name}: " if name else ""
    raise ValueError(f"{prefix}the trace does not start with the start event of its cell.")


def read(path: str | os.PathLike[str], grid_only: bool = False) -> Iterator[dict[str, Any]]:
    """The events of a schedtrace file, see events(), read one at a time, so that memory does not grow with the file."""
    root = _root(grid_only)
    with open(path, "rb") as f:
        if _partial_header(head := f.read(HEADER.size)):
            return
        check_version(version(head), os.fspath(path))
        while len(prefix := f.read(4)) == 4:
            size, pos = _U32.unpack(prefix)[0], f.tell() - 4
            if _corrupt(size, pos, os.fspath(path)):
                return
            buf = f.read(size)
            if len(buf) < size:
                return
            event = _decode(root, buf)
            if event is None:
                _warn_corrupt("event", pos, os.fspath(path))
                return
            yield event

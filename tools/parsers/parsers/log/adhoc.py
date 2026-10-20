# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Ad-hoc metrics of an OCUDU log: the numeric values of a key=value field in its lines, e.g. tbs=5889 of the slot
decision lines of the scheduler, with the time of their log entry; or, without a key, the lines that match a regular
expression, e.g. to mark them as events.

Lines may be kept to those matching a regular expression, and each value may take the value of a split key, the
nearest one before it in its line, e.g. the ue= of the grant a tbs= belongs to. Numbers with a unit are scaled to the
canonical unit of the METRICS lines, e.g. kbps to bps.
"""

from __future__ import annotations

import mmap
import re
from dataclasses import dataclass

from . import preamble
from .chunks import count_newlines
from .metrics import normalize_number

# Characters of a key, as in key=value fields, e.g. c-rnti, cw[0] or dl_bo.
_KEY_CHARS = r"[\w.\-\[\]]"
# Timestamp at the start of an entry line.
_TIMESTAMP_RE = re.compile(preamble.TIMESTAMP_PATTERN.encode())
# Bytes looked back for the header of the entry of a continuation line.
_MAX_LOOKBACK = 1 << 20


# Characters of the text of a line kept for each line of scan_lines().
MAX_TEXT = 500
_UE_RE = re.compile(r"(?<![\w-])ue=(\d+)")
# The UE RNTI tokens of search.Filter, not the RA-RNTI or MsgB-RNTI of a random access, which is no UE's.
_RNTI_RE = re.compile(r"(?<![\w-])(?:c-|tc-|c)?rnti=(0x[0-9a-fA-F]+)")


@dataclass(frozen=True)
class Spec:
    """An ad-hoc metric: the values of key, in the lines that match filter, if any, split by the value of split_key. Without
    a key, the lines that match filter.
    """

    key: str | None
    filter: str | None = None
    split_key: str | None = None

    def __post_init__(self) -> None:
        if self.key is None:
            if not self.filter:
                raise ValueError("Give a key, or a filter of the lines.")
            if self.split_key is not None:
                raise ValueError("A split key needs a key.")
        elif not re.fullmatch(_KEY_CHARS + "+", self.key):
            raise ValueError(f"Invalid key {self.key!r}.")
        if self.split_key is not None and not re.fullmatch(_KEY_CHARS + "+", self.split_key):
            raise ValueError(f"Invalid split key {self.split_key!r}.")
        if self.filter:
            try:
                re.compile(self.filter)
            except re.error as e:
                raise ValueError(f"Invalid filter: {e}.") from None


def value_re(key: str) -> re.Pattern[bytes]:
    """Pattern of the numeric values of a key, with "num" and an optional "unit"."""
    return re.compile(
        rb"(?<![\w.\-\[\]])" + re.escape(key.encode())
        + rb"=(?P<num>[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)(?P<unit>[A-Za-z%]+)?(?![\w.])"
    )


def split_re(key: str) -> re.Pattern[bytes]:
    """Pattern of the values of a split key, up to a space or a separator."""
    return re.compile(rb"(?<![\w.\-\[\]])" + re.escape(key.encode()) + rb"=(?P<value>[^\s,;()\[\]]+)")


def scan(path: str, start: int, end: int, first_line: int, spec: Spec) -> dict:
    """Scans the byte range [start, end) of a log, whose first line is line first_line, for the values of a spec.

    Returns "rows" of (time in seconds since the epoch, line number, line offset, value, split value or None), in
    file order, and "unit", the unit of the values when they have one. Lines without a timestamp take the one of the
    entry they belong to. The spec must have a key; see scan_lines() for one without.
    """
    if spec.key is None:
        raise ValueError("scan() needs a spec with a key; scan_lines() finds the lines of a filter.")
    values = value_re(spec.key)
    splits = split_re(spec.split_key) if spec.split_key else None
    keep = re.compile(spec.filter, re.IGNORECASE) if spec.filter else None
    rows: list[tuple] = []
    unit: str | None = None
    # An empty file, of range (0, 0), cannot be mapped.
    if end <= start:
        return {"rows": rows, "unit": unit}
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        times = _EntryTimes(mm, start)
        line_no, counted_to = first_line, start
        line_start, line, t = -1, b"", None
        # Lines are only read where the key is found, and lines counted between them, which keeps the scan in C.
        for m in values.finditer(mm, start, end):
            pos = m.start()
            newline = mm.rfind(b"\n", start, pos)
            if (newline + 1 if newline != -1 else start) != line_start:
                line_start = newline + 1 if newline != -1 else start
                line_no += count_newlines(mm, counted_to, line_start)
                counted_to = line_start
                line_end = mm.find(b"\n", pos, end)
                # Without the \r of a CRLF line end, so that $ of the filter matches there.
                line = mm[line_start: end if line_end == -1 else line_end].removesuffix(b"\r")
                t = None
                if keep is None or keep.search(line.decode("utf-8", "replace")):
                    t = times.at(line_start)
            if t is None:
                continue
            value, value_unit, _ = normalize_number(m["num"].decode(), m["unit"].decode() if m["unit"] else None)
            if value_unit is not None:
                unit = unit or value_unit
            split = None
            if splits is not None:
                found = None
                for found in splits.finditer(line, 0, pos - line_start):
                    pass
                split = found["value"].decode("utf-8", "replace") if found else None
            rows.append((t, line_no, line_start, value, split))
    return {"rows": rows, "unit": unit}


def scan_lines(path: str, start: int, end: int, first_line: int, spec: Spec) -> list[tuple]:
    """Scans the byte range [start, end) of a log, whose first line is line first_line, for the lines that match the
    filter of a spec, case-insensitive.

    Returns rows of (time in seconds since the epoch, line number, line offset, text of at most MAX_TEXT characters,
    logger, level letter, UE index, RNTI), in file order, the last four None when the line has none.
    """
    pattern = re.compile(crlf_anchors(spec.filter).encode(), re.IGNORECASE | re.MULTILINE)
    rows: list[tuple] = []
    if end <= start:
        return rows
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        times = _EntryTimes(mm, start)
        line_no, counted_to, last_start = first_line, start, -1
        for m in pattern.finditer(mm, start, end):
            newline = mm.rfind(b"\n", start, m.start())
            line_start = newline + 1 if newline != -1 else start
            # A filter that matches empty, e.g. "^", matches at the end too, where the next range starts.
            if line_start == last_start or line_start >= end:
                continue
            last_start = line_start
            line_no += count_newlines(mm, counted_to, line_start)
            counted_to = line_start
            t = times.at(line_start)
            if t is None:
                continue
            line_end = mm.find(b"\n", line_start, end)
            text = mm[line_start: end if line_end == -1 else line_end].removesuffix(b"\r").decode("utf-8", "replace")
            pre = preamble.match_header(text)
            ue = _UE_RE.search(text)
            rnti = _RNTI_RE.search(text)
            rows.append((t, line_no, line_start, text[:MAX_TEXT], pre.group("layer").strip() if pre else None,
                         pre.group("level") if pre else None, int(ue.group(1)) if ue else None,
                         rnti.group(1).lower() if rnti else None))
    return rows


def crlf_anchors(pattern: str) -> str:
    """A regular expression whose $ anchors also match before the CR of a CRLF line end, which re does not."""
    out: list[str] = []
    i, n, in_class = 0, len(pattern), False
    while i < n:
        c = pattern[i]
        if c == "\\":
            out.append(pattern[i: i + 2])
            i += 2
            continue
        if in_class:
            in_class = c != "]"
        elif c == "[":
            # A ] first in a set, after its ^ if any, is a literal.
            j = i + 1 + pattern.startswith("^", i + 1)
            j += pattern.startswith("]", j)
            out.append(pattern[i:j])
            i, in_class = j, True
            continue
        elif pattern.startswith("(?#", i):
            # A comment ends at the first ), so its text is kept as it is.
            j = pattern.find(")", i)
            j = n if j == -1 else j + 1
            out.append(pattern[i:j])
            i = j
            continue
        elif c == "$":
            out.append(r"(?=\r?$)")
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


class _EntryTimes:
    """Times of the entries of the lines of a range of a log, looked up in file order."""

    def __init__(self, mm: mmap.mmap, floor: int):
        self._mm = mm
        self._floor = floor
        # Start of the line looked up last, and its time.
        self._line = -1
        self._t: float | None = None

    def at(self, line_start: int) -> float | None:
        """The time of the entry of the line at line_start: its own timestamp, else that of the nearest line before it
        with one, looking back at most _MAX_LOOKBACK bytes and not before the floor, the start of a range at an entry.
        """
        self._t = self._find(line_start)
        self._line = line_start
        return self._t

    def _find(self, line_start: int) -> float | None:
        mm = self._mm
        pos = line_start
        lowest = max(self._floor, line_start - _MAX_LOOKBACK)
        while True:
            if m := _TIMESTAMP_RE.match(mm, pos):
                return preamble.to_epoch(m.group().decode())
            # The lines from the one looked up last belong to its entry, so the walk back stops there.
            if pos == self._line:
                return self._t
            if pos <= lowest:
                return None
            newline = mm.rfind(b"\n", lowest, pos - 1)
            pos = newline + 1 if newline != -1 else lowest

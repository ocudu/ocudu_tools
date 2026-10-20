# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Search of the entries of OCUDU logs by logger, level, UE, RNTI, PCI, time and pattern.

An entry is a timestamped line and the continuation lines below it, e.g. an ASN.1 body or a SIB1 dump, and filters
match anywhere in it. The configuration echo at the top of a log is left out unless asked for.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from ..ran.rnti import parse_any
from . import config, preamble

# Start of a bound that has a date, rather than being a time of day.
_DATE_RE = re.compile(r"\d{4}-\d\d-\d\d")


@dataclass
class Filter:
    """Conditions that an entry must all meet. Unset ones match any entry."""

    # Logger, e.g. RRC, matched exactly but for case.
    layer: str | None = None
    # Regex that the level letter, e.g. "E|W", must match whole.
    level: str | None = None
    # UE index, matched as a "ue=N" token.
    ue: str | None = None
    # RNTI in any form of ran.rnti.parse_any(), e.g. 4601, 0x4601 or 17921, matched as "rnti=0x", "c-rnti=0x",
    # "tc-rnti=0x" or "crnti=0x" tokens.
    rnti: str | None = None
    # PCI, matched as a "pci=N" token.
    pci: str | None = None
    # Bounds of the time of the entry, inclusive: ISO dates or timestamps, with "T" or a space as str(datetime) gives,
    # or times of day such as HH:MM:SS.mmm, compared to the same precision.
    after: str | None = None
    before: str | None = None
    # Regex searched in the entry, ignoring case, with ^ and $ at its lines.
    pattern: str | None = None

    def __post_init__(self) -> None:
        self.after, self.before = iso_bound(self.after), iso_bound(self.before)
        self._level = re.compile(self.level) if self.level else None
        self._ue = re.compile(rf"\bue={re.escape(self.ue)}\b") if self.ue is not None else None
        rnti = parse_any(self.rnti) if self.rnti is not None else None
        if self.rnti is not None and rnti is None:
            raise ValueError(f"Not an RNTI: {self.rnti!r}.")
        # Hex digits without the leading zeros, which logs may or may not print.
        digits = rnti[2:].lstrip("0") or "0" if rnti else ""
        # Not the RA-RNTI or MsgB-RNTI of a random access, which is no UE's.
        self._rnti = re.compile(rf"(?<![\w-])(?:c-|tc-|c)?rnti=0x0*{digits}\b", re.IGNORECASE) if rnti else None
        self._pci = re.compile(rf"\bpci={re.escape(self.pci)}\b") if self.pci is not None else None
        self._pattern = re.compile(self.pattern, re.IGNORECASE | re.MULTILINE) if self.pattern else None

    def matches(self, entry: list[str]) -> bool:
        """Whether an entry of entries() meets the conditions."""
        m = preamble.match_header(entry[0])
        if not m:
            return False
        ts = m.group("timestamp")
        if self.after and not _at_or_after(ts, self.after):
            return False
        if self.before and not _at_or_before(ts, self.before):
            return False
        if self.layer and m.group("layer").strip().upper() != self.layer.upper():
            return False
        if self._level and not self._level.fullmatch(m.group("level") or ""):
            return False
        text = "\n".join(entry)
        return all(p is None or p.search(text) for p in (self._ue, self._rnti, self._pci, self._pattern))


def entries(lines: Iterable[str], include_config_echo: bool = False) -> Iterator[list[str]]:
    """The entries of a log, each as its lines without line ends, LF or CRLF, empty lines left out, and lines before
    the first entry too.
    """
    entry: list[str] | None = None
    skipping = False
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line:
            continue
        if m := preamble.match_header(line):
            if entry is not None:
                yield entry
            skipping = not include_config_echo and _is_config_echo(m, line)
            entry = None if skipping else [line]
        elif entry is not None and not skipping:
            entry.append(line)
    if entry is not None:
        yield entry


def search(lines: Iterable[str], flt: Filter, include_config_echo: bool = False) -> Iterator[str]:
    """The text of the entries of a log that meet a filter."""
    for entry in entries(lines, include_config_echo):
        if flt.matches(entry):
            yield "\n".join(entry)


def _is_config_echo(m: re.Match, line: str) -> bool:
    """Whether an entry line is the header of the configuration echo, whose body is the effective configuration."""
    return m.group("layer").strip() == "CONFIG" and config.is_echo_header(line)


def iso_bound(ref: str | None) -> str | None:
    """A time bound of Filter with its date and time separated by "T", e.g. "2026-04-21T18:18:30" for
    "2026-04-21 18:18:30" of str(datetime). Other bounds, e.g. dates or times of day, are returned as they are.
    """
    if ref and len(ref) > 10 and ref[10] == " " and _DATE_RE.match(ref):
        return ref[:10] + "T" + ref[11:]
    return ref


def _at_or_after(ts: str, ref: str) -> bool:
    return _at_precision(ts, ref) >= ref


def _at_or_before(ts: str, ref: str) -> bool:
    return _at_precision(ts, ref) <= ref


def _at_precision(ts: str, ref: str) -> str:
    """A timestamp cut to the precision of a bound, an ISO date or timestamp or a time of day."""
    return (ts if _DATE_RE.match(ref) else ts.partition("T")[2])[: len(ref)]

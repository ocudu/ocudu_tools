# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Shared helpers for the analyze-ran-log `correlate` scripts.

These helpers exist only for the *cross-correlation* work that spans artifact
sources. Per-artifact parsing detail belongs in the per-type subtrees
(`../pcap/`, `../ocudu/`, `../amari-ue/`), not here.

Provides:
- the per-session shared cache root (prefix files written here with `correlate-`)
- UTC-aware timestamp parsing for the three log clocks (gnb.log ISO,
  ue.log / mme.log HH:MM:SS.mmm + `# Started on` date anchor, pcap epoch)
- SFN.slot parsing and the regexes that join PHY radio events across sources
- a lenient testbed.json parser (the file is a Python repr, not JSON)

Clock model (see references/cross-correlation.md):
- gnb.log, ue.log, mme.log all print UTC; pcap frame.time_epoch is UTC seconds.
  They share one wall-clock, so cross-source time compares directly — but
  capinfos/tshark *display* in local TZ, so always use raw frame.time_epoch.
- PHY (SFN.slot, RNTI) is the exact cross-source join for
  PUSCH/PUCCH/PDCCH/PDSCH. MAC/SCHED UL events lag the PHY slot by a
  processing delay; some builds expose the true PHY slot as `slot_rx=`.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import os
import re
from pathlib import Path

# --------------------------------------------------------------------------
# Per-session shared cache root (shared across the analyze-ran-log type
# scripts). Write cross-correlation spills here with a `correlate-` prefix. The
# OS reaps /tmp on reboot — no manual cleanup.
# --------------------------------------------------------------------------

CACHE_ROOT = (
    Path(os.environ.get("CLAUDE_CODE_TMPDIR", "/tmp"))
    / f"claude-skills-{os.environ.get('CLAUDE_CODE_SESSION_ID', 'default')}"
)


def cache_path(input_path, tag: str, suffix: str = "txt") -> Path:
    """Deterministic cache path: correlate-<sha>.<suffix> under CACHE_ROOT."""
    canonical = str(Path(input_path).resolve())
    digest = hashlib.sha256(f"{canonical}\0{tag}".encode()).hexdigest()[:16]
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    return CACHE_ROOT / f"correlate-{digest}.{suffix}"


# --------------------------------------------------------------------------
# Timestamp parsing (everything normalised to a naive UTC datetime)
# --------------------------------------------------------------------------

# gnb.log: 2026-04-29T14:27:21.265863  (ISO-8601, UTC, microseconds)
GNB_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+")
# ue.log / mme.log line clock: 14:27:21.273  (HH:MM:SS.mmm, UTC, no date)
HMS_TS_RE = re.compile(r"\b(\d{2}:\d{2}:\d{2}\.\d+)")
# ue.log / mme.log header anchor: "# Started on 2026-04-29 14:27:19"
STARTED_ON_RE = re.compile(r"#\s*Started on\s+(\d{4}-\d{2}-\d{2})\s")


def parse_gnb_ts(s: str) -> _dt.datetime:
    """Parse a gnb.log ISO timestamp to a naive UTC datetime."""
    return _dt.datetime.fromisoformat(s)


def parse_hms_ts(s: str, ref_date: str) -> _dt.datetime:
    """Parse an HH:MM:SS.mmm clock (ue.log/mme.log) using a YYYY-MM-DD anchor."""
    return _dt.datetime.fromisoformat(f"{ref_date}T{s}")


def epoch_to_utc(epoch) -> _dt.datetime:
    """Pcap frame.time_epoch (UTC seconds) -> naive UTC datetime."""
    return _dt.datetime.fromtimestamp(float(epoch), tz=_dt.timezone.utc).replace(tzinfo=None)


def started_on_date(log_path) -> str | None:
    """Read the `# Started on YYYY-MM-DD ...` date anchor from a ue.log/mme.log header."""
    try:
        with open(log_path, errors="replace") as f:
            for _ in range(40):
                line = f.readline()
                if not line:
                    break
                m = STARTED_ON_RE.search(line)
                if m:
                    return m.group(1)
    except OSError:
        return None
    return None


def first_gnb_event_ts(gnb_log) -> str | None:
    """First ISO timestamp in a gnb.log (the first real event, after the build line)."""
    try:
        with open(gnb_log, errors="replace") as f:
            for line in f:
                if line[:1].isdigit():
                    m = GNB_TS_RE.match(line)
                    if m:
                        return m.group(0)
    except OSError:
        return None
    return None


# --------------------------------------------------------------------------
# SFN.slot — the clock-independent radio key
# --------------------------------------------------------------------------

def slot_key(sfn_slot: str) -> tuple[int, int] | None:
    """'123.14' -> (123, 14). Returns None if unparseable."""
    try:
        a, b = sfn_slot.split(".")
        return int(a), int(b)
    except (ValueError, AttributeError):
        return None


# --------------------------------------------------------------------------
# PHY radio-event regexes.
#
# gNB side (gnb.log), e.g.:
#   2026-04-29T14:27:21.281595 [PHY     ] [I] [  123.14] PUSCH: rnti=0x4601 ... crc=KO iter=6.0 sinr=97.0dB ...
#   ... [PHY     ] [I] [  122.19] PRACH: rsi=1 ... detected_preambles=[{idx=41 ...}] ...
# UE side (ue.log), e.g.:
#   14:27:21.273 [PHY] UL 0035 00 4601  123.14 PUSCH: harq=0 prb=34:3 ... tb_len=11 ...
#   14:27:20.082 [PHY] UL 0001 00    -   32.19 PRACH: sequence_index=10 ...
# --------------------------------------------------------------------------

GNB_PHY_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+)\s+\[PHY\s*\]\s+\[\w\]\s+"
    r"\[\s*(?P<slot>\d+\.\d+)\]\s+(?P<kind>PRACH|PUSCH|PUCCH|PDCCH|PDSCH):\s*(?P<rest>.*)$"
)
GNB_RNTI_RE = re.compile(r"\brnti=(?P<rnti>0x[0-9a-fA-F]+)")
GNB_CRC_RE = re.compile(r"\bcrc=(?P<crc>OK|KO)")
GNB_SINR_RE = re.compile(r"\bsinr=(?P<sinr>\S+)")
GNB_ITER_RE = re.compile(r"\biter=(?P<iter>\S+)")
GNB_PREAMBLE_RE = re.compile(r"idx=(?P<idx>\d+)")
# A PUCCH UCI field. sr=no and a payload of 2s, e.g. ack=2 or csi1=2222, carry no valid UCI.
GNB_PUCCH_UCI_RE = re.compile(r"\b(?P<field>ack|sr|csi1|csi2)=(?P<value>\S+)")
# Some builds expose the true PHY reception slot of a later MAC/SCHED line:
SLOT_RX_RE = re.compile(r"\bslot_rx=(?P<slot_rx>\d+\.\d+)")

UE_PHY_RE = re.compile(
    r"^(?P<ts>\d{2}:\d{2}:\d{2}\.\d+)\s+\[PHY\]\s+(?P<dir>UL|DL)\s+"
    r"(?P<ueid>[0-9a-fA-F]+)\s+(?P<cc>\d+)\s+(?P<rnti>[0-9a-fA-F]+|-)\s+"
    r"(?P<slot>\d+\.\d+)\s+(?P<kind>PRACH|PUSCH|PUCCH|PDCCH|PDSCH):\s*(?P<rest>.*)$"
)
UE_SEQIDX_RE = re.compile(r"sequence_index=(?P<seq>\d+)")


# --------------------------------------------------------------------------
# testbed.json — a Python repr (OrderedDict/NodeInfo), NOT JSON.
# --------------------------------------------------------------------------

_TESTBED_ENTRY_RE = re.compile(
    r"'(?P<name>[A-Za-z0-9_.-]+)'\s*,?\s*NodeInfo\(address='(?P<addr>[^']*)'\s*,\s*port=(?P<port>\d+)",
    re.DOTALL,
)


def parse_testbed(testbed_path) -> dict[str, dict]:
    """Map component name -> {'address': ip, 'port': int} from a testbed.json repr.

    Returns {} if the file is missing or unparseable.
    """
    out: dict[str, dict] = {}
    try:
        text = Path(testbed_path).read_text(errors="replace")
    except OSError:
        return out
    for m in _TESTBED_ENTRY_RE.finditer(text):
        out[m.group("name")] = {"address": m.group("addr"), "port": int(m.group("port"))}
    return out

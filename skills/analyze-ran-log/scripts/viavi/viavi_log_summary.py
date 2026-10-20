#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
viavi_log_summary.py - Summarize a VIAVI RU-simulator command log.

Single streaming pass over a TM500-style "Command Log" (the artifact the VIAVI
RU simulator emits while driving an OCUDU test). Emits a compact, token-efficient
summary: test setup, UE population, random-access / connection / registration
tallies, mobility, GETSTATS throughput and BLER maxima across all dumps (per-UE
and COMBINED dumps reported apart), and anomalies.

Usage:
  python3 viavi_log_summary.py <path>

<path> can be:
  - a *_Command_Log*.txt file directly
  - the matching *_Command_Log*.zip (same content, read in place)
  - a directory containing one (newest is used)

The .txt and .zip carry identical content; given a directory, the .txt of a
basename is preferred over its .zip. This module also exposes resolve_log() /
open_log() / is_viavi_head() / READ_ERRORS for the sibling resolve.py and
viavi_log_search.py scripts.
"""

from __future__ import annotations

import io
import itertools
import re
import sys
import zipfile
import zlib
from collections import Counter
from datetime import datetime
from pathlib import Path

# A line that opens a record: "DD/MM/YY HH:MM:SS:mmm <rest>". The shutdown phase
# inserts a literal "... " between the timestamp and the payload, so allow it.
TS_RE = re.compile(r"^(\d{2}/\d{2}/\d{2}) (\d{2}:\d{2}:\d{2}:\d{3})(?: \.\.\.)? (.*)")
# Markers that identify the artifact from its first lines (any one suffices). A
# log cut before the RSET only holds TMAE info lines and PORT/CONNECT/ABOT replies.
SIGNATURE_RE = re.compile(
    r"(C: RSET 0x|I: CMPI |I: TMAE 0x|C: (PORT|CONNECT|ABOT) 0x"
    r"|^\d{2}/\d{2}/\d{2} [\d:]+ RSET\b)",
    re.M,
)
# Errors of opening or reading a log, a broken .zip included.
READ_ERRORS = (OSError, zipfile.BadZipFile, EOFError, zlib.error)


# ---------------------------------------------------------------------------
# Resolution / IO (shared with resolve.py and viavi_log_search.py)
# ---------------------------------------------------------------------------

def _command_logs(d: Path) -> list[Path]:
    """Command-log files under a directory, one per basename (.txt over .zip)."""
    by_stem: dict[str, Path] = {}
    for p in d.rglob("*Command_Log*"):
        ext = p.suffix.lower()
        if not p.is_file() or ext not in (".txt", ".zip"):
            continue
        prev = by_stem.get(p.stem)
        if prev is None or (ext == ".txt" and prev.suffix.lower() != ".txt"):
            by_stem[p.stem] = p
    return list(by_stem.values())


def resolve_log(path_str: str) -> Path:
    """Resolve an input to the VIAVI command-log file (.txt or .zip)."""
    p = Path(path_str).expanduser().resolve()
    if p.is_file():
        return p
    if p.is_dir():
        logs = _command_logs(p)
        if logs:
            # Newest by name (timestamp prefix sorts lexicographically).
            return sorted(logs, key=lambda f: f.name)[-1]
    raise FileNotFoundError(f"No *Command_Log*.txt/.zip found at or under {p}")


def _zip_member(zf: zipfile.ZipFile, path: Path) -> str:
    names = [n for n in zf.namelist() if n.lower().endswith(".txt")]
    names = names or zf.namelist()
    if not names:
        raise zipfile.BadZipFile(f"{path.name} holds no file")
    return names[0]


def open_log(path: Path):
    """Open a command log for line iteration (transparently handles .zip).

    Raises one of READ_ERRORS on an unreadable file or a broken .zip.
    """
    if path.suffix.lower() == ".zip":
        try:
            zf = zipfile.ZipFile(path)
        except zipfile.BadZipFile as e:
            raise zipfile.BadZipFile(f"{path.name}: {e}") from None
        raw = zf.open(_zip_member(zf, path))
        stream = io.TextIOWrapper(raw, encoding="utf-8", errors="replace")
        stream._viavi_zip = zf  # keep the archive alive for the stream's lifetime
        return stream
    return open(path, encoding="utf-8", errors="replace")


def is_viavi_head(path: Path, n_lines: int = 60) -> bool:
    """True if the first lines carry the VIAVI command-log signature."""
    try:
        with open_log(path) as f:
            # islice, not next() in a genexpr: under PEP 479 a short file would
            # surface as RuntimeError and escape the handler below.
            head = "".join(itertools.islice(f, n_lines))
    except READ_ERRORS:
        head = ""
        try:
            with open_log(path) as f:
                head = f.read(8192)
        except READ_ERRORS:
            return False
    return bool(SIGNATURE_RE.search(head))


# ---------------------------------------------------------------------------
# Timestamp helper
# ---------------------------------------------------------------------------

def parse_ts(date_s: str, time_s: str) -> datetime | None:
    """Parse 'DD/MM/YY' + 'HH:MM:SS:mmm' (day-first, colon before ms)."""
    try:
        hms, ms = time_s.rsplit(":", 1)
        return datetime.strptime(f"{date_s} {hms}.{ms}", "%d/%m/%y %H:%M:%S.%f")
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

CMPI_EVENTS = {
    "ra_init": "L2 Random Access Initiated",
    "ra_done": "L2 Random Access Complete",
    "ra_err": "L2 Random Access Error",
    "ra_cancel": "L2 Random Access Cancelled",
    "conn": "NR CONNECTION IND",
    "disc": "NR DISCONNECTION IND",
    "conn_fail": "NR CONNECTION FAILED IND",
    "reg": "NR REGISTRATION IND",
    "dereg": "NR DEREGISTRATION IND",
    "pdu_mod": "NR PDU SESSION MODIFICATION IND",
    "ho": "RRC Handover Complete",
    "reest": "RRC Connection Re-establishment",
    "reest_drop": "RRC Re-establishment Dropped",
    "reest_err": "RRC Re-establishment Error",
    "rlc_max": "RLC maximum retransmissions reached",
    "plmn_loss": "NR PLMN LOSS IND",
    "lower_fail": "NR LOWER LAYER FAILURE IND",
}
# Events whose UE Ids the anomalies list.
OFFENDER_EVENTS = ("ra_err", "conn_fail", "lower_fail", "reest_drop", "reest_err")

UE_ID_RE = re.compile(r"UE Id:\s*(\d+)")
RA_RESULT_RE = re.compile(r"Result:\s*([A-Za-z_]+)")
AVG_TPUT_RE = re.compile(r"Average:\s*(\d+)")
BLER_RE = re.compile(r"(?<![\d.])(\d\.\d{6,})")
SCS_RE = re.compile(r"\bCFGR\s+\d+\s+SCS\s+(\d+)")
DLFREQ_RE = re.compile(r"DL Freq:\s*([\d.]+)\s*MHz")
CELLID_RE = re.compile(r"Cell ID:\s*(\d+)")
# GETSTATS header: one UE ("UE ID: 0") or a COMBINED range ("UE ID: 0-32").
STATS_UE_RE = re.compile(r"UE ID:\s*\d+(-\d+)?\s+\(Radio Context")
# "ORAN_RU RU1: ORAN: CU PLANE ACTIVE" or "ORAN_RU ORAN: CU PLANE ACTIVE (Ru 0 on server)".
RU_INDEX_RE = re.compile(r"\(Ru (\d+)")
RU_NAME_RE = re.compile(r"ORAN_RU\s+(\S+):")


def parse(stream) -> dict:
    r = {
        "first_dt": None, "last_dt": None,
        "first_ts": None, "last_ts": None,
        "contexts": [],          # [(idx, rat)] from SCXT
        "mts_mode": False,
        "rus": set(),            # RU names or "Ru N" indexes that reached CU PLANE ACTIVE
        "scs": set(),            # CFGR SCS index values
        "dl_freqs": set(),       # MHz seen in GETSTATS headers
        "cell_ids": set(),       # Cell IDs seen in GETSTATS headers
        "ue_ids": set(),
        "ev": Counter(),         # CMPI event tallies (keys of CMPI_EVENTS)
        "ra_results": Counter(),
        "offender_ues": {},      # event key -> set of UE Ids seen on that event
        "getstats_dumps": 0,
        "cmd_fail": [],          # [(ts, cmd, code)] for C: <cmd> 0xNN != 0x00
        "tma_warn": 0,
        # GETSTATS aggregates across all dumps (each dump covers a UE subset, so
        # report peaks/maxima rather than one unrepresentative dump), keyed by
        # record kind: "ue" (one UE) or "combined" (a UE ID range).
        "stat_records": Counter(),
        "peak_dl": Counter(), "peak_ul": Counter(),
        "max_dl_bler": Counter(), "max_ul_bler": Counter(),
    }
    cur_sec = None          # 'DL' | 'UL' within a UE's SCH stats
    bler_pending = None     # 'DL' | 'UL' — capture BLER on next data row
    kind = "ue"             # record kind of the current GETSTATS header

    for raw in stream:
        line = raw.rstrip("\r\n")
        if not line:
            continue

        m = TS_RE.match(line)
        ts = payload = None
        if m:
            date_s, ts, payload = m.group(1), m.group(2), m.group(3)
            dt = parse_ts(date_s, ts)
            if dt:
                if r["first_dt"] is None:
                    r["first_dt"], r["first_ts"] = dt, ts
                r["last_dt"], r["last_ts"] = dt, ts

        # Continuation (no timestamp) and timestamped stat lines both feed the
        # GETSTATS accumulators; CMPI events only appear on timestamped lines.
        body = payload if payload is not None else line

        # --- CMPI / control events (timestamped only) ---
        if payload is not None:
            if payload.startswith("I: CMPI"):
                for key, needle in CMPI_EVENTS.items():
                    if needle in payload:
                        r["ev"][key] += 1
                        if key == "ra_err":
                            mr = RA_RESULT_RE.search(payload)
                            if mr:
                                r["ra_results"][mr.group(1)] += 1
                        if key in OFFENDER_EVENTS:
                            # Record which UEs hit it -- a count alone can't be scoped.
                            mo = UE_ID_RE.search(payload)
                            if mo:
                                r["offender_ues"].setdefault(key, set()).add(int(mo.group(1)))
                        break
                mu = UE_ID_RE.search(payload)
                if mu:
                    r["ue_ids"].add(int(mu.group(1)))
                if "CU PLANE ACTIVE" in payload:
                    mi = RU_INDEX_RE.search(payload)
                    mn = RU_NAME_RE.search(payload)
                    if mi:
                        r["rus"].add(f"Ru {mi.group(1)}")
                    elif mn and mn.group(1) != "ORAN":
                        r["rus"].add(mn.group(1))
            elif payload.startswith("I: TMAE") and "Warning" in payload:
                r["tma_warn"] += 1
            elif payload.startswith("C: FORW") and "GETSTATS" in payload:
                r["getstats_dumps"] += 1
                cur_sec = bler_pending = None
            elif payload.startswith("C: "):
                cm = re.match(r"C:\s+(\S+)\s+0x([0-9A-Fa-f]+)", payload)
                if cm and cm.group(2) != "00":
                    r["cmd_fail"].append((ts, cm.group(1), "0x" + cm.group(2)))
            elif payload.startswith("SCXT"):
                cs = re.match(r"SCXT\s+(\d+)\s+(\S+)", payload)
                if cs:
                    r["contexts"].append((int(cs.group(1)), cs.group(2)))
            elif payload.startswith("SCFG") and "MTS_MODE" in payload:
                r["mts_mode"] = True
            ms = SCS_RE.search(payload)
            if ms:
                r["scs"].add(int(ms.group(1)))

        # --- GETSTATS block (timestamped UE headers + continuation stat rows) ---
        mh = STATS_UE_RE.search(body)
        if mh:
            kind = "combined" if mh.group(1) else "ue"
            r["stat_records"][kind] += 1
            mf = DLFREQ_RE.search(body)
            if mf:
                r["dl_freqs"].add(mf.group(1))
            mc = CELLID_RE.search(body)
            if mc:
                r["cell_ids"].add(int(mc.group(1)))
            cur_sec = bler_pending = None
        elif "DL-SCH (PCC)" in body:
            cur_sec = "DL"
        elif "UL-SCH (PCC)" in body:
            cur_sec = "UL"
        elif "HARQ (PCC)" in body:
            cur_sec = bler_pending = None   # stop SCH capture before HARQ tables
        elif body.lstrip().startswith("Throughput:") and cur_sec in ("DL", "UL"):
            ma = AVG_TPUT_RE.search(body)
            if ma:
                key = "peak_dl" if cur_sec == "DL" else "peak_ul"
                r[key][kind] = max(r[key][kind], int(ma.group(1)))
        elif "[BLER]" in body and cur_sec in ("DL", "UL"):
            bler_pending = cur_sec
        elif bler_pending and BLER_RE.search(body):
            bl = float(BLER_RE.search(body).group(1))
            key = "max_dl_bler" if bler_pending == "DL" else "max_ul_bler"
            r[key][kind] = max(r[key][kind], bl)
            bler_pending = None

    return r


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _fmt_duration(sec: float) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}h{m:02d}m{s:02d}s" if h else f"{m:d}m{s:02d}s"


def summarize(path_str: str) -> None:
    log_path = resolve_log(path_str)
    via_zip = log_path.suffix.lower() == ".zip"
    with open_log(log_path) as f:
        r = parse(f)

    print(f"Command log : {log_path}" + ("  (read from .zip)" if via_zip else ""))
    print()

    # ----- Test setup -----
    print("=== Test Setup ===")
    if r["first_ts"] and r["last_ts"]:
        print(f"  First event : {r['first_ts']}")
        print(f"  Last event  : {r['last_ts']}")
        if r["first_dt"] and r["last_dt"]:
            print(f"  Duration    : {_fmt_duration((r['last_dt'] - r['first_dt']).total_seconds())}")
    if r["contexts"]:
        ctx = ", ".join(f"{i}:{rat}" for i, rat in dict(r["contexts"]).items())
        print(f"  Contexts    : {ctx}")
    if r["mts_mode"]:
        print("  Mode        : MTS (multi-test scenario)")
    if r["rus"]:
        print(f"  RUs active  : {', '.join(sorted(r['rus']))}  (ORAN: CU PLANE ACTIVE)")
    if r["scs"]:
        print(f"  SCS index   : {', '.join(str(s) for s in sorted(r['scs']))}")
    if r["cell_ids"]:
        print(f"  Cell IDs    : {', '.join(str(c) for c in sorted(r['cell_ids']))}")
    if r["dl_freqs"]:
        print(f"  DL freqs    : {', '.join(sorted(r['dl_freqs']))} MHz")
    print()

    # ----- UE population -----
    print("=== UE Population ===")
    if r["ue_ids"]:
        ids = sorted(r["ue_ids"])
        print(f"  Distinct UE Ids : {len(ids)} (range {ids[0]}–{ids[-1]})")
    else:
        print("  Distinct UE Ids : 0")
    print()

    # ----- Random access -----
    ev = r["ev"]
    print("=== Random Access ===")
    init, done, err = ev["ra_init"], ev["ra_done"], ev["ra_err"]
    print(f"  Initiated : {init}")
    print(f"  Complete  : {done}" + (f"  ({100.0 * done / init:.1f}% of initiated)" if init else ""))
    print(f"  Error     : {err}")
    if ev["ra_cancel"]:
        print(f"  Cancelled : {ev['ra_cancel']}")
    for res, n in r["ra_results"].most_common():
        print(f"     - {n} × {res}")
    print()

    # ----- Connection / registration -----
    print("=== Connection / Registration ===")
    print(f"  CONNECTION IND        : {ev['conn']}")
    print(f"  DISCONNECTION IND     : {ev['disc']}")
    print(f"  CONNECTION FAILED IND : {ev['conn_fail']}")
    print(f"  REGISTRATION IND      : {ev['reg']}")
    print(f"  DEREGISTRATION IND    : {ev['dereg']}")
    if ev["lower_fail"]:
        print(f"  LOWER LAYER FAILURE IND: {ev['lower_fail']}")
    if ev["pdu_mod"]:
        print(f"  PDU SESSION MOD IND   : {ev['pdu_mod']}")
    print()

    # ----- Mobility -----
    if ev["ho"] or ev["reest"] or ev["reest_drop"] or ev["reest_err"]:
        print("=== Mobility ===")
        print(f"  Handover Complete          : {ev['ho']}")
        print(f"  Re-establishment Started   : {ev['reest']}")
        print(f"  Re-establishment Dropped   : {ev['reest_drop']}")
        print(f"  Re-establishment Error     : {ev['reest_err']}")
        print()

    # ----- Throughput / BLER (GETSTATS aggregates) -----
    print("=== Throughput / BLER (GETSTATS) ===")
    if r["getstats_dumps"]:
        recs = r["stat_records"]
        print(f"  GETSTATS dumps    : {r['getstats_dumps']}  ({recs['ue']} per-UE, "
              f"{recs['combined']} COMBINED stat records)")
        labels = {"ue": "per-UE", "combined": "COMBINED, all UEs"}
        for kind in ("ue", "combined"):
            if not recs[kind]:
                continue
            print(f"  [{labels[kind]}, max across records]")
            print(f"    Peak DL avg tput : {r['peak_dl'][kind]} (as reported by the tester)")
            print(f"    Peak UL avg tput : {r['peak_ul'][kind]} (as reported by the tester)")
            print(f"    Max DL-SCH BLER  : {r['max_dl_bler'][kind]:.4f}")
            print(f"    Max UL-SCH BLER  : {r['max_ul_bler'][kind]:.4f}")
    else:
        print("  (no GETSTATS dumps found)")
    print()

    # ----- Errors / warnings -----
    print("=== Errors / Warnings ===")
    print(f"  Failed command responses (C: ... 0x!=00) : {len(r['cmd_fail'])}")
    for ts, cmd, code in r["cmd_fail"][:10]:
        print(f"     - {ts}  {cmd} {code}")
    print(f"  TMAE warnings        : {r['tma_warn']}")
    print(f"  RLC max retransmits  : {ev['rlc_max']}")
    print()

    # ----- Anomalies -----
    anomalies = []

    def offenders(key):
        ids = sorted(r["offender_ues"].get(key, ()))
        if not ids:
            return ""
        shown = ", ".join(str(i) for i in ids[:10])
        return f" [UE Ids: {shown}" + (f" +{len(ids) - 10} more]" if len(ids) > 10 else "]")

    if err:
        anomalies.append(f"{err} random-access errors"
                         + (f" ({', '.join(f'{n} {res}' for res, n in r['ra_results'].most_common())})"
                            if r["ra_results"] else "")
                         + offenders("ra_err"))
    if ev["conn_fail"]:
        anomalies.append(f"{ev['conn_fail']} NR CONNECTION FAILED IND" + offenders("conn_fail"))
    if ev["lower_fail"]:
        anomalies.append(f"{ev['lower_fail']} NR LOWER LAYER FAILURE IND" + offenders("lower_fail"))
    if ev["reest_drop"]:
        anomalies.append(f"{ev['reest_drop']} RRC Re-establishment Dropped" + offenders("reest_drop"))
    if ev["reest_err"]:
        anomalies.append(f"{ev['reest_err']} RRC Re-establishment Error" + offenders("reest_err"))
    if ev["rlc_max"]:
        anomalies.append(f"{ev['rlc_max']} RLC max-retransmission events")
    if r["cmd_fail"]:
        anomalies.append(f"{len(r['cmd_fail'])} command responses not 0x00")

    print("=== Anomalies ===")
    if anomalies:
        for a in anomalies:
            print(f"  ! {a}")
    else:
        print("  None")
    print()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <command-log.txt | .zip | dir>", file=sys.stderr)
        sys.exit(1)
    try:
        summarize(sys.argv[1])
    except READ_ERRORS as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

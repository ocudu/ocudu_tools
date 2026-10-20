#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
ue_log_summary.py - Summarize an Amarisoft UE run directory.

Single-pass parse of ue.log + stdout.log + amarisoft_ue.cfg.
Emits a compact, token-efficient summary suitable for AI context.

Usage:
  python3 ue_log_summary.py <path>

<path> can be:
  - a UE log file directly (ue.log, ue0.log, ...)
  - Run directory containing ue.log (e.g. 2026-05-18_18-17-34/)
  - Component directory amarisoft-ue-N/ (finds latest timestamp subdirectory)
  - Retina test directory test_gnb[...] (finds latest amarisoft-ue-N/ subdir)
"""

import itertools
import re
import sys
from collections import Counter
from pathlib import Path


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

PRIMARY = "ue.log"
# First lines of a ue.log: its "#" header or a timestamped record.
HEAD_RE = re.compile(
    r"^(# (lteue\b|Started on |Log file format)|\d{2}:\d{2}:\d{2}\.\d{3} \[\w+\] )", re.M
)


def is_ue_log_head(path: Path, n_lines: int = 60) -> bool:
    """True if the first lines of path look like an Amarisoft UE log."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = "".join(itertools.islice(f, n_lines))
    except OSError:
        return False
    return bool(HEAD_RE.search(head))


def resolve_ue_log(path_str: str) -> Path:
    """The UE log of an input: the file itself, or the latest ue.log at or under a directory."""
    p = Path(path_str).resolve()

    if p.is_file():
        if is_ue_log_head(p) or p.stat().st_size == 0:
            return p
        # A sibling artifact (stdout.log, amarisoft_ue.cfg) stands for its run directory.
        if (p.parent / PRIMARY).is_file() and p.name != PRIMARY:
            return p.parent / PRIMARY
        raise FileNotFoundError(f"{p} does not look like an Amarisoft UE log")

    if (p / PRIMARY).is_file():
        return p / PRIMARY

    # Lexicographic order of the YYYY-MM-DD_HH-MM-SS run subdirs is time order.
    candidates = sorted(f for f in p.rglob(PRIMARY) if f.is_file())
    if candidates:
        return candidates[-1]

    others = sorted(f.name for f in p.glob("ue*.log") if f.is_file())
    hint = f" (found {', '.join(others)}: pass the file)" if others else ""
    raise FileNotFoundError(f"No {PRIMARY} found under {p}{hint}")


def resolve_run_dir(path_str: str) -> Path:
    return resolve_ue_log(path_str).parent


# ---------------------------------------------------------------------------
# Config parser (amarisoft_ue.cfg)
# ---------------------------------------------------------------------------

def parse_cfg(cfg_path: Path) -> dict:
    result = {
        "ue_count": 1,
        "n_ue_groups": 0,
        "bands": [],
        "bandwidths": [],
        "n_cells": 0,
        "imsi": None,
        "n_imsi": 0,
        "sim_events": [],
    }
    if not cfg_path.exists():
        return result

    # Strip comments line-by-line. A whole-file DOTALL /* */ regex is unsafe here
    # because RF-driver URLs like "tcp://*:31000" contain the substring "/*",
    # which a non-greedy match pairs with a later "*/", deleting the cells block.
    lines = []
    for line in cfg_path.read_text(errors="replace").splitlines():
        if line.lstrip().startswith("//"):
            continue
        line = re.sub(r"/\*.*?\*/", "", line)  # single-line block comments only
        lines.append(line)
    text = "\n".join(lines)

    # A cfg can define many UE groups (one `ue_count` per imsi/group block, e.g.
    # 500 distinct imsi entries each with ue_count:1). The total simulated UE
    # count is the SUM across all groups, not just the first match.
    group_counts = [int(x) for x in re.findall(r"ue_count\s*:\s*(\d+)", text)]
    result["n_ue_groups"] = len(group_counts)
    result["ue_count"] = sum(group_counts) if group_counts else 1

    result["bands"] = [int(x) for x in re.findall(r"\bband\s*:\s*(\d+)", text)]
    result["bandwidths"] = [float(x) for x in re.findall(r"\bbandwidth\s*:\s*([\d.]+)", text)]
    result["n_cells"] = len(re.findall(r"\brf_port\s*:", text))

    imsis = re.findall(r'imsi\s*:\s*"([^"]+)"', text)
    result["n_imsi"] = len(imsis)
    if imsis:
        result["imsi"] = imsis[0]

    seen = set()
    for m in re.finditer(r'event\s*:\s*"([^"]+)"', text):
        ev = m.group(1)
        if ev not in seen:
            result["sim_events"].append(ev)
            seen.add(ev)

    return result


# ---------------------------------------------------------------------------
# ue.log parser
# ---------------------------------------------------------------------------

LINE_RE = re.compile(r"^(\d{2}:\d{2}:\d{2}\.\d{3}) \[(\w+)\] (\S+)\s+(.*)")
NAS_STATE_RE = re.compile(r"(\S+)\s+New state\s*:\s*(\S+)\s+(\S+)")
PROD_EVENT_RE = re.compile(r"SIM-Event:\s*(\S+)")
# RRC rest = "<ue_or_sfn> <cell_id> <channel>: <msg>" (direction already consumed by LINE_RE)
RRC_MSG_RE = re.compile(r"(\S+)\s+(\S+)\s+([\w\-]+):\s*(.*)")
# A handover shows up as the ASN.1 body structure "reconfigurationWithSync {".
# Match the brace, not the bare word — diagnostic lines (e.g. "PDCP reestablish ...
# without reconfigurationWithSync ... ignore it") mention the word but are not HOs.
RECONFIG_SYNC_RE = re.compile(r"reconfigurationWithSync\s*\{")
# Failure wording of record headers, e.g. "Random access failure",
# "Contention resolution failed or timer expired", "Registration reject".
ERROR_RE = re.compile(r"\b(fail(ed|ure)?|error|expired|reject)\b", re.IGNORECASE)


def parse_ue_log(ue_log: Path) -> dict:
    result = {
        "log_start_date": None,
        "log_end_date": None,
        "start_time": None,
        "end_time": None,
        "nas_states": [],       # [(ts, ue_id, gmm_state, cm_state)]
        "rrc_events": [],       # [(ts, dir, cell, channel, msg_type)]
        "prod_events": [],      # [(ts, event_type)]
        "prach_count": 0,
        "ho_count": 0,          # reconfigurationWithSync occurrences
        "reest_count": 0,       # RRC reestablishment requests seen
        "crc_fail_count": 0,
        "error_count": 0,
        "ue_ids": set(),
        "cells": set(),
    }

    if not ue_log.exists():
        return result

    with open(ue_log, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\r\n")
            if not line:
                continue

            # Header comments
            if line.startswith("#"):
                if "Started on" in line:
                    m = re.search(r"Started on (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
                    if m:
                        result["log_start_date"] = m.group(1)
                elif "Ended on" in line:
                    m = re.search(r"Ended on (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
                    if m:
                        result["log_end_date"] = m.group(1)
                continue

            # Count handovers via the ASN.1 body structure "reconfigurationWithSync {"
            # (a continuation line). The bare word also appears in diagnostic prose,
            # so matching the brace avoids false positives.
            if RECONFIG_SYNC_RE.search(line):
                result["ho_count"] += 1
                continue

            m = LINE_RE.match(line)
            if not m:
                continue

            ts = m.group(1)
            layer = m.group(2)
            rest = m.group(4)

            # ue.log has no severity field. PHY crc=FAIL is counted on its own.
            if layer != "PHY" and ERROR_RE.search(rest):
                result["error_count"] += 1

            if result["start_time"] is None:
                result["start_time"] = ts
            result["end_time"] = ts

            if layer == "NAS":
                nas_m = NAS_STATE_RE.match(rest)
                if nas_m:
                    uid, gmm, cm = nas_m.group(1), nas_m.group(2), nas_m.group(3)
                    result["nas_states"].append((ts, uid, gmm, cm))
                    if re.match(r"^[0-9a-fA-F]{4}$", uid):  # UE IDs are hex (e.g. 000a, 0080)
                        result["ue_ids"].add(uid)

            elif layer == "PROD":
                ev_m = PROD_EVENT_RE.search(rest)
                if ev_m:
                    result["prod_events"].append((ts, ev_m.group(1)))

            elif layer == "RRC":
                rrc_m = RRC_MSG_RE.match(rest)
                if rrc_m:
                    direction = m.group(3)  # DL/UL from main line regex
                    cell = rrc_m.group(2)
                    channel = rrc_m.group(3)
                    msg_type = rrc_m.group(4).strip()
                    result["rrc_events"].append((ts, direction, cell, channel, msg_type))
                    if re.match(r"^\d{2}$", cell):
                        result["cells"].add(cell)
                    if "reestablishment request" in msg_type.lower():
                        result["reest_count"] += 1

            elif layer == "PHY":
                if "PRACH:" in rest:
                    result["prach_count"] += 1
                if "crc=FAIL" in rest:
                    result["crc_fail_count"] += 1

    result["ue_ids"] = sorted(result["ue_ids"])
    result["cells"] = sorted(result["cells"])
    return result


# ---------------------------------------------------------------------------
# stdout.log parser
# ---------------------------------------------------------------------------

def parse_stdout(stdout_log: Path) -> dict:
    result = {
        "ue_version": None,
        "rf_ports": [],
        "cbr_recv": [],   # [(addr, sent, recv)]
        "cbr_send": [],   # [(addr, sent, recv)]
        "cells_sib": [],  # [0, 1, ...]
        "warnings": [],
    }
    if not stdout_log.exists():
        return result

    with open(stdout_log, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue

            if "UE version" in line:
                m = re.search(r"UE version ([\d\-]+)", line)
                if m:
                    result["ue_version"] = m.group(1)

            elif line.startswith("RF"):
                result["rf_ports"].append(line)

            elif "CBR_RECV:" in line:
                m = re.search(r"\[(.+)\] CBR_RECV: sent (\d+), recv (\d+)", line)
                if m:
                    result["cbr_recv"].append((m.group(1), int(m.group(2)), int(m.group(3))))

            elif "CBR_SEND:" in line:
                m = re.search(r"\[(.+)\] CBR_SEND: sent (\d+), recv (\d+)", line)
                if m:
                    result["cbr_send"].append((m.group(1), int(m.group(2)), int(m.group(3))))

            elif "SIB found" in line:
                m = re.search(r"Cell (\d+): SIB found", line)
                if m:
                    result["cells_sib"].append(int(m.group(1)))

            elif line.startswith("Warning"):
                # Only keep non-trivial warnings (skip "unused property" spam)
                if "unused property" not in line and "hyperthreading" not in line:
                    result["warnings"].append(line)

    return result


# ---------------------------------------------------------------------------
# Duration helper
# ---------------------------------------------------------------------------

def ts_delta_seconds(t1: str, t2: str) -> float | None:
    """Seconds from t1 to the next occurrence of t2, both HH:MM:SS.mmm."""
    try:
        def to_sec(t):
            h, m, s = t.split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
        # The clock has no date, so an end before the start crossed midnight.
        return (to_sec(t2) - to_sec(t1)) % 86400
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Main summary output
# ---------------------------------------------------------------------------

def summarize(path_str: str):
    ue_log = resolve_ue_log(path_str)
    run_dir = ue_log.parent
    stdout_log = run_dir / "stdout.log"
    cfg_path = run_dir / "amarisoft_ue.cfg"

    print(f"Run directory : {run_dir}")
    if ue_log.name != PRIMARY:
        print(f"UE log        : {ue_log.name}")
    print()

    cfg = parse_cfg(cfg_path)
    log = parse_ue_log(ue_log)
    out = parse_stdout(stdout_log)

    # ----- Configuration -----
    print("=== UE Configuration ===")
    mode = "multi-UE" if cfg["ue_count"] > 1 else "single-UE"
    print(f"  UE count    : {cfg['ue_count']} ({mode})")
    if cfg["n_ue_groups"] > 1:
        print(f"  UE groups   : {cfg['n_ue_groups']} (imsi/ue_count blocks summed for the total above)")
    if cfg["imsi"]:
        suffix = f" (first of {cfg['n_imsi']})" if cfg["n_imsi"] > 1 else ""
        print(f"  IMSI        : {cfg['imsi']}{suffix}")
    if cfg["bands"]:
        print(f"  Bands       : {', '.join('n' + str(b) for b in sorted(set(cfg['bands'])))}")
    if cfg["bandwidths"]:
        print(f"  Bandwidths  : {', '.join(str(b) + ' MHz' for b in sorted(set(cfg['bandwidths'])))}")
    if cfg["n_cells"] > 1:
        print(f"  Cells       : {cfg['n_cells']} (configured in cell_groups)")
    if cfg["sim_events"]:
        print(f"  Sim events  : {' → '.join(cfg['sim_events'])}")
    print()

    # ----- Run info -----
    print("=== Run Info ===")
    if log["log_start_date"]:
        print(f"  Started     : {log['log_start_date']}")
    elif log["start_time"]:
        print(f"  First log ts: {log['start_time']}")
    if log["log_end_date"]:
        print(f"  Ended       : {log['log_end_date']}")
    else:
        print(f"  Ended       : (no clean exit marker — possible crash/kill)")
    if log["start_time"] and log["end_time"]:
        delta = ts_delta_seconds(log["start_time"], log["end_time"])
        if delta is not None and delta >= 0:
            print(f"  Duration    : {delta:.1f} s")
    if out["ue_version"]:
        print(f"  UE version  : {out['ue_version']}")
    for rf in out["rf_ports"]:
        print(f"  {rf}")
    if out["cells_sib"]:
        print(f"  Cells found : {', '.join('Cell ' + str(c) for c in out['cells_sib'])}")
    print()

    # ----- NAS state: per-line timeline (single UE) or aggregate (multi-UE) -----
    multi_ue = cfg["ue_count"] > 1 or len(log["ue_ids"]) > 1
    if not log["nas_states"]:
        print("=== NAS State Timeline ===")
        print("  (no NAS state transitions found)")
        print()
    elif multi_ue:
        # Per-UE timelines would flood output; aggregate by final state instead.
        print("=== NAS State Summary (multi-UE) ===")
        final_per_ue = {}
        reached_registered = set()
        for ts, uid, gmm, cm in log["nas_states"]:
            final_per_ue[uid] = f"{gmm} {cm}"
            if gmm == "5GMM-REGISTERED":
                reached_registered.add(uid)
        print(f"  UEs seen           : {len(final_per_ue)}")
        print(f"  Reached REGISTERED : {len(reached_registered)}")
        print("  Final state breakdown:")
        for state, n in Counter(final_per_ue.values()).most_common():
            print(f"    {n:4d} ×  {state}")
        print()
    else:
        print("=== NAS State Timeline ===")
        prev = None
        for ts, uid, gmm, cm in log["nas_states"]:
            state = f"{gmm} {cm}"
            if state != prev:
                print(f"  {ts}  [{uid}]  {state}")
                prev = state
        print()

    # ----- Key RRC events: timeline (single UE) or counts (multi-UE) -----
    # Note: the AS-security messages are logged WITHOUT an "RRC " prefix
    # ("Security mode command" / "Security mode complete"); the rest carry it.
    KEY_RRC = {
        "MIB", "SIB1",
        "RRC setup", "RRC setup complete",
        "Security mode command", "Security mode complete",
        "RRC reconfiguration", "RRC reconfiguration complete",
        "RRC reestablishment", "RRC reestablishment request",
        "RRC reestablishment complete",
        "RRC release", "RRC reject",
    }
    key_events = [
        (ts, direction, cell, channel, msg_type)
        for ts, direction, cell, channel, msg_type in log["rrc_events"]
        if channel.startswith("BCCH") or any(k.lower() in msg_type.lower() for k in KEY_RRC)
    ]
    if multi_ue:
        # 256 UEs interleave; a timeline is noise. Count by message type instead.
        print("=== Key RRC Message Counts (multi-UE) ===")
        counts = Counter(f"{d} {ch}: {mt}" for _, d, _, ch, mt in key_events)
        for label, n in counts.most_common():
            print(f"  {n:5d} ×  {label}")
        if not counts:
            print("  (no key RRC messages found)")
    else:
        print("=== Key RRC Events ===")
        MAX_RRC_SHOWN = 20
        for ts, direction, cell, channel, msg_type in key_events[:MAX_RRC_SHOWN]:
            print(f"  {ts}  {direction}  CL{cell}  {channel}: {msg_type}")
        if len(key_events) > MAX_RRC_SHOWN:
            print(f"  ... ({len(key_events) - MAX_RRC_SHOWN} more — use ue_log_search.py for full list)")
        if not key_events:
            print("  (no key RRC messages found)")
    print()

    # ----- Procedure summary -----
    print("=== Procedure Summary ===")
    print(f"  PRACH attempts     : {log['prach_count']}")
    print(f"  Handovers (sync)   : {log['ho_count']}")
    print(f"  Reestablishments   : {log['reest_count']}")
    print(f"  PHY CRC failures   : {log['crc_fail_count']}")
    print(f"  Error log lines    : {log['error_count']}")
    if log["nas_states"] and not multi_ue:
        _, _, gmm, cm = log["nas_states"][-1]
        print(f"  Final NAS state    : {gmm} {cm}")
    print()

    # ----- Traffic stats -----
    if out["cbr_recv"] or out["cbr_send"]:
        print("=== Traffic Stats ===")
        for addr, sent, recv in out["cbr_recv"]:
            loss = sent - recv
            pct = 100.0 * loss / sent if sent else 0.0
            flag = "  !" if pct > 1.0 else "   "
            print(f"{flag} CBR_RECV [{addr}]: sent={sent}, recv={recv},"
                  f" loss={loss} ({pct:.2f}%)")
        for addr, sent, recv in out["cbr_send"]:
            loss = sent - recv
            pct = 100.0 * loss / sent if sent else 0.0
            flag = "  !" if pct > 1.0 else "   "
            print(f"{flag} CBR_SEND [{addr}]: sent={sent}, recv={recv},"
                  f" loss={loss} ({pct:.2f}%)")
        print()

    # ----- Sim events: timeline (single UE) or counts (multi-UE) -----
    if log["prod_events"]:
        if multi_ue:
            print("=== Sim Event Counts (multi-UE) ===")
            for ev, n in Counter(e for _, e in log["prod_events"]).most_common():
                print(f"  {n:5d} ×  {ev}")
        else:
            print("=== Sim Events Timeline ===")
            for ts, ev in log["prod_events"]:
                print(f"  {ts}  {ev}")
        print()

    # ----- Anomalies -----
    anomalies = []
    if log["crc_fail_count"] > 10:
        anomalies.append(f"High PHY CRC failures: {log['crc_fail_count']}")
    if log["error_count"] > 0:
        anomalies.append(f"Error log lines: {log['error_count']}")
    for addr, sent, recv in out["cbr_recv"] + out["cbr_send"]:
        if sent > 0:
            pct = 100.0 * (sent - recv) / sent
            if pct > 1.0:
                anomalies.append(f"Packet loss {addr}: {pct:.1f}% ({sent-recv}/{sent})")
    if not log["log_end_date"]:
        anomalies.append("No clean exit marker in ue.log (possible crash)")
    if log["nas_states"] and not multi_ue:
        final_gmm = log["nas_states"][-1][2]
        all_gmm_states = {s[2] for s in log["nas_states"]}
        prod_types = [e for _, e in log["prod_events"]]
        # Unexpected NULL: quit event missing
        if "NULL" in final_gmm and "quit" not in prod_types:
            anomalies.append(f"UE ended in {final_gmm} without quit sim event")
        # Never registered: power_on fired but UE never reached 5GMM-REGISTERED.
        # Use exact match — "5GMM-DEREGISTERED" also contains the substring "REGISTERED".
        if "5GMM-REGISTERED" not in all_gmm_states and "power_on" in prod_types:
            anomalies.append(f"UE never reached 5GMM-REGISTERED (final: {final_gmm})")
    elif log["nas_states"] and multi_ue:
        # Per-UE: how many of the UEs that appeared ever reached 5GMM-REGISTERED?
        # Exact token match — "5GMM-DEREGISTERED" also contains "REGISTERED".
        seen_ues, registered_ues = set(), set()
        for _, uid, gmm, _ in log["nas_states"]:
            seen_ues.add(uid)
            if gmm == "5GMM-REGISTERED":
                registered_ues.add(uid)
        unregistered = sorted(seen_ues - registered_ues)
        never = len(unregistered)
        if never > 0:
            # Name the offenders: "scope by UE ID early" is unusable without them.
            shown = ", ".join(unregistered[:10])
            more = f" (+{never - 10} more)" if never > 10 else ""
            anomalies.append(
                f"{never} of {len(seen_ues)} UEs never reached 5GMM-REGISTERED: "
                f"{shown}{more}"
            )
    if out["warnings"]:
        for w in out["warnings"]:
            anomalies.append(f"Stdout warning: {w[:100]}")

    if anomalies:
        print("=== Anomalies ===")
        for a in anomalies:
            print(f"  ! {a}")
        print()
    else:
        print("=== Anomalies ===")
        print("  None")
        print()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <ue.log | run-dir | component-dir | test-dir>",
              file=sys.stderr)
        sys.exit(1)
    try:
        summarize(sys.argv[1])
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

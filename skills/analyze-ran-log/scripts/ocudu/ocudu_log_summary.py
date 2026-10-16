#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
ocudu_log_summary.py — Summarize an OCUDU gNB run directory.

Single-pass parse of the OCUDU app log (gnb/du/cu/cu_cp/cu_up) + stdout.log
+ ocudu_*.yml + metrics.json, with the parsers.log package of this repo.
Emits a compact, token-efficient summary suitable for AI context.

Usage:
  python3 ocudu_log_summary.py <path>

<path> can be:
  - gnb.log file directly
  - Run directory containing gnb.log (e.g. 2026-05-18_18-18-27/)
  - Component directory ocudu-gnb-N-M/ (finds latest timestamp subdirectory)
  - Retina test directory test_gnb[...] (finds latest ocudu-gnb-N-M/ subdir)
"""

from __future__ import annotations

import datetime
import argparse
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent / "_lib"))

from parsers.log import config, console, metrics_json, summary  # noqa: E402
from parsers.log.run import CONFIG_GLOB, primary_log_in, resolve_run_dir  # noqa: E402

# Log level options in display order.
LEVEL_OPTIONS = ("all", "lib", "rrc", "ngap", "f1ap", "e1ap", "cu", "pdcp", "mac", "du", "phy", "sec", "xnap", "rlc",
                 "gtpu", "f1u", "sdap", "ofh", "fapi", "e2ap", "config")
# Node types of config.AppConfig, as shown.
NODE_TYPES = {"gNB": "monolithic gNB", "CU": "CU (CP+UP)"}
# Loggers of the procedure counts, which log them at info or debug.
PROCEDURE_LOGGERS = ("RRC", "NGAP", "CU-CP", "CU-CP-E1")
# Entry headers of info or debug lines of the procedure loggers, and of METRICS rows, which have no level, unlike
# the start and stop lines of the metrics workers.
_VERBOSE_RE = re.compile(r"\[(RRC|NGAP|CU-CP|CU-CP-E1)\s*\] \[[ID]\] ")
_METRICS_ROW_RE = re.compile(r"\d \[METRICS\s*\] (?!\[[A-Z]\] )")
# Characters of a line that hold its entry header.
_HEADER_LEN = 64


def parse_cfg(cfg_path: Path, gnb_log: Path) -> dict:
    """The configuration of a run, from its configuration file, else from the echo in its log."""
    cfg = None
    if cfg_path.exists():
        cfg = config.parse_config(cfg_path.read_text(errors="replace"))
    elif gnb_log.exists():
        with open(gnb_log, encoding="utf-8", errors="replace") as f:
            cfg = config.from_log(f)
    found = cfg is not None
    cfg = cfg or config.AppConfig(planes=[])
    levels = {k: cfg.levels[k] for k in LEVEL_OPTIONS if k in cfg.levels}
    # MAC details, SCHED lines (gated by mac_level, there is no sched_level) and DU proc=... traces need info.
    quiet = ("none", "warning", "error")
    suppressed = "DU" in cfg.planes and (cfg.level("mac") in quiet or cfg.level("du") in quiet)
    return {
        "raw": cfg.values,
        "pcaps": cfg.pcaps,
        "log_levels": levels,
        "node_type": NODE_TYPES.get(cfg.node_type, cfg.node_type),
        "planes": cfg.planes,
        "l2_traces_suppressed": suppressed,
        # Unknown without a configuration file or echo.
        "tdd": cfg.tdd if found else None,
        "amf_addrs": cfg.amf_addrs,
        "amf_port": cfg.amf_port,
        "amf_bind_addrs": cfg.amf_bind_addrs,
    }


def parse_gnb_log(gnb_log: Path) -> dict:
    """The summary.LogSummary of a log, with the procedure loggers without info or debug lines (silent_loggers), and
    metrics_rows and last_metrics of METRICS rows only.
    """
    if not gnb_log.exists():
        return {**vars(summary.LogSummary()), "silent_loggers": list(PROCEDURE_LOGGERS)}
    verbose: set[str] = set()
    rows = {"metrics_rows": 0, "last_metrics": None}

    def scan(lines):
        for raw in lines:
            if len(verbose) < len(PROCEDURE_LOGGERS) and (m := _VERBOSE_RE.search(raw, 0, _HEADER_LEN)):
                verbose.add(m.group(1))
            if m := _METRICS_ROW_RE.search(raw, 0, _HEADER_LEN):
                rows["metrics_rows"] += 1
                rows["last_metrics"] = raw[m.end() :].rstrip("\r\n")
            yield raw

    with open(gnb_log, encoding="utf-8", errors="replace") as f:
        out = vars(summary.summarize(scan(f)))
    return {**out, **rows, "silent_loggers": [name for name in PROCEDURE_LOGGERS if name not in verbose]}


def parse_stdout(stdout_log: Path) -> dict:
    if not stdout_log.exists():
        return vars(console.ConsoleSummary())
    with open(stdout_log, encoding="utf-8", errors="replace") as f:
        return vars(console.summarize(f))


def parse_metrics(metrics_path: Path) -> dict:
    if not metrics_path.exists():
        return vars(metrics_json.MetricsRollup())
    return vars(metrics_json.rollup(metrics_json.load_records(metrics_path)))


# ---------------------------------------------------------------------------
# Duration helper
# ---------------------------------------------------------------------------

def ts_delta_seconds(t1: str, t2: str) -> float | None:
    """Compute seconds between two ISO-8601 timestamps with microsecond precision."""
    try:
        def to_sec(t):
            date, tod = t.split("T")
            y, mo, d = (int(x) for x in date.split("-"))
            h, m, s = tod.split(":")
            # Days-since-epoch keeps the delta correct across a midnight boundary.
            days = datetime.date(y, mo, d).toordinal()
            return days * 86400 + int(h) * 3600 + int(m) * 60 + float(s)
        return to_sec(t2) - to_sec(t1)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def summarize(path_str: str) -> None:
    src = Path(path_str)
    if src.is_file():
        gnb_log, run_dir = src, src.parent      # accept any log filename (gnb (1).log, srsdu.log, ...)
    else:
        run_dir = resolve_run_dir(path_str)
        gnb_log = primary_log_in(run_dir) or run_dir / "gnb.log"
    stdout_log = run_dir / "stdout.log"
    # Config name tracks the role (ocudu_gnb.yml / ocudu_du.yml / ocudu_cu_cp.yml …).
    cfg_path = run_dir / "ocudu_gnb.yml"
    if not cfg_path.is_file():
        cfg_path = next(iter(sorted(run_dir.glob(CONFIG_GLOB))), cfg_path)
    metrics_path = run_dir / "metrics.json"

    print(f"Run directory : {run_dir}")
    if gnb_log.name != "gnb.log":
        print(f"Log file      : {gnb_log.name}")
    if cfg_path.name != "ocudu_gnb.yml" and cfg_path.is_file():
        print(f"Config file   : {cfg_path.name}")
    print()

    cfg = parse_cfg(cfg_path, gnb_log)
    log = parse_gnb_log(gnb_log)
    out = parse_stdout(stdout_log)
    met = parse_metrics(metrics_path)

    # ----- Build -----
    print("=== Build ===")
    if log["build_commit"]:
        print(f"  Commit / branch : {log['build_commit']} / {log['build_branch']}")
    if out["banner_commit"] and out["banner_commit"] != log["build_commit"]:
        print(f"  stdout banner   : {out['banner_commit']}")
    print()

    # ----- Configuration -----
    print("=== Configuration ===")
    raw = cfg["raw"]
    if cfg["node_type"]:
        planes = f"  (planes: {', '.join(cfg['planes'])})" if cfg["planes"] else ""
        print(f"  Node type     : {cfg['node_type']}{planes}")
    if "gnb_id" in raw:
        print(f"  gnb_id        : {raw['gnb_id']}")
    if "ran_node_name" in raw:
        print(f"  ran_node_name : {raw['ran_node_name']}")
    cell_bits = []
    if "pci" in raw:
        cell_bits.append(f"pci={raw['pci']}")
    if "band" in raw:
        cell_bits.append(f"band=n{raw['band']}")
    if "channel_bandwidth_MHz" in raw:
        cell_bits.append(f"bw={raw['channel_bandwidth_MHz']} MHz")
    if "common_scs" in raw:
        cell_bits.append(f"scs={raw['common_scs']} kHz")
    if "dl_arfcn" in raw:
        cell_bits.append(f"dl_arfcn={raw['dl_arfcn']}")
    if "nof_antennas_dl" in raw and "nof_antennas_ul" in raw:
        cell_bits.append(f"{raw['nof_antennas_dl']}T{raw['nof_antennas_ul']}R")
    if cell_bits:
        print(f"  Cell cfg      : {' '.join(cell_bits)}")
    if cfg["tdd"] is not None:
        print(f"  Duplex        : {'TDD' if cfg['tdd'] else 'FDD'}")
    else:
        print("  Duplex        : unknown (no configuration file or echo)")
    if cfg["amf_addrs"]:
        port = cfg["amf_port"] or 38412
        print(f"  AMF           : {cfg['amf_addrs'][0]}:{port}"
              f" (bind={cfg['amf_bind_addrs'][0] if cfg['amf_bind_addrs'] else '?'})")
    if cfg["log_levels"]:
        levels = ", ".join(f"{k}={v}" for k, v in cfg["log_levels"].items())
        print(f"  Log levels    : {levels}")
    if cfg["l2_traces_suppressed"]:
        print("  L2 traces     : MAC/SCHED (gated by mac_level) + DU proc traces "
              "suppressed (warning) -> per-UE proc=... lines absent; "
              "DU/MAC latency profiling unavailable")
    if cfg["pcaps"]:
        print(f"  PCAPs enabled : {', '.join(cfg['pcaps'])}")
    print()

    # ----- Run lifecycle -----
    print("=== Run Lifecycle ===")
    if log["first_ts"]:
        print(f"  First log ts        : {log['first_ts']}")
    if log["amf_connected_ts"]:
        print(f"  AMF SCTP connected  : {log['amf_connected_ts']}")
    if log["ng_setup"]:
        print(f"  NG setup            : {log['ng_setup']}")
    if log["last_ts"]:
        print(f"  Last log ts         : {log['last_ts']}")
    if log["first_ts"] and log["last_ts"]:
        d = ts_delta_seconds(log["first_ts"], log["last_ts"])
        if d is not None and d >= 0:
            print(f"  Duration            : {d:.1f} s")
    print(f"  Clean shutdown      : {'yes' if log['clean_shutdown'] else 'no — abnormal exit'}")
    if out["started"] and not out["stopped"]:
        print("  stdout marker       : started but no `Stopping...` line")
    print()

    # ----- Cells / UEs -----
    print("=== Cells & UEs ===")
    if log["cells_created"]:
        for ts, pci in log["cells_created"]:
            print(f"  {ts}  cell pci={pci} created")
    elif log["pcis_seen"]:
        print(f"  PCIs seen           : {', '.join(str(p) for p in log['pcis_seen'])}")
    if len(log["ue_creates"]) <= 10:
        for ts, ue, rnti in log["ue_creates"]:
            print(f"  {ts}  ue={ue} c-rnti=0x{rnti} created")
    else:
        print(f"  UE creations        : {len(log['ue_creates'])} (first @ {log['ue_creates'][0][0]},"
              f" last @ {log['ue_creates'][-1][0]})")
    if log["init_ctx_done"]:
        done = len(log["init_ctx_done"])
        attempted = len(log["ue_creates"]) or done
        print(f"  Initial Context Setup OK : {done}/{attempted}")
    if log["ue_releases"]:
        if len(log["ue_releases"]) <= 5:
            for ts, ue in log["ue_releases"]:
                print(f"  {ts}  ue={ue} released")
        else:
            print(f"  UE releases         : {len(log['ue_releases'])}")
    print()

    # ----- Procedures -----
    print("=== Procedures ===")
    print(f"  RRC reconfigs       : {log['rrc_reconfigs']}")
    print(f"  Handovers (Reconf w/Sync) : {log['ho_count']}")
    print(f"  NGAP handover msgs  : {log['ngap_handover_count']}")
    print(f"  Reestablishments    : {log['reest_count']}")
    print(f"  Bearer setups       : {log['bearer_setups']}")
    print(f"  Bearer modifications: {log['bearer_modifications']}")
    print(f"  Bearer releases     : {log['bearer_releases']}")
    print(f"  PRACH events        : {log['prach_events']}")
    print(f"  PHY CRC failures (crc=KO) : {log['crc_fails']}")
    # Caveat: these counters come from RRC/NGAP/F1AP/E1AP/CU layers. If those are
    # logged at 'warning', the layers are silent and the counts read 0 even when
    # the procedures happened — say so rather than implying nothing occurred.
    muted = [name for name, lvl in cfg["log_levels"].items()
             if lvl == "warning" and name in ("rrc", "ngap", "f1ap", "e1ap", "cu")]
    if muted:
        print(f"  NOTE: {', '.join(muted)} logged at 'warning' — the above procedure "
              f"counts are unreliable (layers silent). Infer HO from SCHED PRACH on the "
              f"target cell / metrics 'ue_reconf' events, or enable info logging.")
    elif not cfg["log_levels"] and log["silent_loggers"]:
        # Without the levels of a configuration, a logger without info or debug lines is taken as logged at warning.
        print(f"  NOTE: no info or debug lines of {', '.join(log['silent_loggers'])} — the above procedure "
              f"counts are unreliable (likely logged at 'warning'). Infer HO from SCHED PRACH on the "
              f"target cell / metrics 'ue_reconf' events, or enable info logging.")
    print()

    # ----- Scheduler metrics rollup -----
    if met["records"]:
        print("=== Scheduler Metrics (from metrics.json) ===")
        print(f"  Records             : {met['records']}")
        if not met["cell_records"]:
            print("  (no record has OCUDU scheduler cell metrics; the file may be of another format, e.g. srsRAN)")
        print(f"  Peak DL bitrate/UE  : {met['peak_dl_brate']/1e6:.2f} Mbps")
        print(f"  Peak UL bitrate/UE  : {met['peak_ul_brate']/1e6:.2f} Mbps")
        print(f"  Max latency         : {met['max_latency_us']} us")
        print(f"  Late DL HARQs       : {met['late_dl_harqs']}")
        print(f"  Late UL HARQs       : {met['late_ul_harqs']}")
        print(f"  Failed PDCCH allocs : {met['failed_pdcch']}")
        print(f"  Failed UCI allocs   : {met['failed_uci']}")
        print(f"  Error indications   : {met['errors']}")
        print(f"  Msg3 ok/nok         : {met['msg3_ok']}/{met['msg3_nok']}"
              + ("  (high nok = RACH contention)" if met['msg3_nok'] else ""))
        if met["event_counts"]:
            evs = ", ".join(f"{n}×{k}" for k, n in met["event_counts"].most_common())
            print(f"  Events              : {evs}")
        print()
    elif log["metrics_rows"]:
        print("=== Scheduler Metrics (from [METRICS] log) ===")
        print(f"  Metrics rows in gnb.log : {log['metrics_rows']}")
        if log["last_metrics"]:
            print(f"  Last row:")
            print(f"    {log['last_metrics'][:200]}")
        print()

    # ----- Anomalies -----
    anomalies = []
    if not log["clean_shutdown"]:
        anomalies.append("No `Workers stopped successfully` line — abnormal exit")
    if log["ng_setup"] == "failure":
        anomalies.append("NGAP NG setup failed")
    elif log["ng_setup"] is None and log["amf_connected_ts"]:
        anomalies.append("NGSetupResponse not seen even though SCTP connected to AMF")
    if log["reest_count"] > 0:
        anomalies.append(f"RRC reestablishment requests: {log['reest_count']}")
    # UEs that got a context but never finished Initial Context Setup.
    if log["ue_creates"] and len(log["init_ctx_done"]) < len(log["ue_creates"]):
        anomalies.append(
            f"Initial Context Setup incomplete: {len(log['init_ctx_done'])}/"
            f"{len(log['ue_creates'])} UEs finished (rest may have been mid-attach "
            f"at capture end, or failed)"
        )
    if log["crc_fails"] > 100:
        anomalies.append(f"High PHY CRC failures (crc=KO): {log['crc_fails']}"
                         f" — may include Msg3 RACH contention in multi-UE runs")
    if met.get("msg3_nok", 0) > 0:
        total = met["msg3_ok"] + met["msg3_nok"]
        anomalies.append(f"Msg3 RACH failures: {met['msg3_nok']}/{total} "
                         f"(contention or coverage)")
    if log["errors"]:
        anomalies.append(f"Error-level log lines: {log['error_count']}"
                         f" (first: [{log['errors'][0][1]}] {log['errors'][0][2][:80]})")
    if log["warnings"]:
        anomalies.append(
            f"Warning-level log lines: {log['warning_count']}"
            f" (first: [{log['warnings'][0][1]}] {log['warnings'][0][2][:80]})"
        )
    if met["late_dl_harqs"] or met["late_ul_harqs"]:
        anomalies.append(f"Late HARQs: dl={met['late_dl_harqs']} ul={met['late_ul_harqs']}")
    if met["failed_pdcch"]:
        anomalies.append(f"Failed PDCCH allocs: {met['failed_pdcch']}")
    if met["errors"]:
        anomalies.append(f"MAC/scheduler error_indications: {met['errors']}")
    # UEs created but not released cleanly (and the run did stop)
    if log["ue_creates"] and log["clean_shutdown"]:
        n_create = len(log["ue_creates"])
        n_release = len(log["ue_releases"])
        if n_release < n_create:
            anomalies.append(
                f"UE releases ({n_release}) < UE creations ({n_create})"
            )

    print("=== Anomalies ===")
    if anomalies:
        for a in anomalies:
            print(f"  ! {a}")
    else:
        print("  None")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Summary of an OCUDU run: build, configuration, UE lifecycle, scheduler "
                                             "metrics and anomalies.")
    ap.add_argument("path", help="gnb.log, du.log or cu*.log, or a run, component or test directory")
    args = ap.parse_args()
    try:
        summarize(args.path)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
align_clocks.py — Confirm and calibrate the cross-source clock relationships for
one OCUDU run, so radio/CP correlation can trust its keys.

It checks three things empirically:
  1. log <-> pcap : the NGSetupRequest timestamp of the gNB, CU or CU-CP log vs
     the first frame epoch of the NGAP pcap beside it (or --pcap). On split runs
     this is the CU log, not the DU log of check 2. One process writes both, so Δ ~0 is guaranteed;
     this is a sanity / display-TZ guard (capinfos/tshark only *display* local TZ),
     NOT an offset measurement. Off-host sources (VIAVI, remote 5GC) are not checked.
  2. UE <-> gNB   : a co-identified PHY PUSCH (same SFN.slot + RNTI) in both
     ue.log and gnb.log -> the SFN.slot must match exactly (no PHY SFN offset);
     the wall-clock delta is the gNB decode-log latency, not clock skew.
  3. slot_rx=     : whether MAC lines expose the true PHY reception slot (some
     builds only) -> when present, MAC<->PHY joins exactly without calibration.

Usage:
    python3 align_clocks.py <test-or-run-dir> [--gnb PATH] [--ue PATH] [--cu PATH] [--pcap PATH]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import utils
import correlate_radio
import resolve

# OCUDU roles that write NGAP, in the order their logs are picked for the log <-> pcap check.
NGAP_ROLES = ("gnb", "cu", "cu-cp", "ocu")


def stage_for_tshark(pcap: Path) -> Path:
    """Copy a pcap under the shared /tmp cache if it's outside /tmp (AppArmor on
    Ubuntu restricts tshark reads to /tmp). Pass through if already under /tmp."""
    pcap = pcap.resolve()
    if str(pcap).startswith("/tmp/"):
        return pcap
    dst = utils.cache_path(pcap, "stage", suffix="pcap")
    if not dst.exists():
        try:
            dst.hardlink_to(pcap)
        except (OSError, AttributeError):
            shutil.copy2(pcap, dst)
    return dst


def first_ngap_frame_epoch(pcap: Path):
    try:
        staged = stage_for_tshark(pcap)
        out = subprocess.run(
            ["tshark", "-r", str(staged), "-Y", "ngap", "-c", "1",
             "-T", "fields", "-e", "frame.time_epoch"],
            capture_output=True, text=True, timeout=60,
        )
        lines = [l for l in out.stdout.splitlines() if l.strip()]
        return float(lines[0]) if lines else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def gnb_ngsetup_ts(gnb_log: Path):
    with open(gnb_log, errors="replace") as f:
        for line in f:
            if "NGSetupRequest" in line and "NGAP" in line:
                m = utils.GNB_TS_RE.match(line)
                if m:
                    return m.group(0)
    return None


def resolve_ngap_log(path_str, cu_arg, gnb_log):
    """(log of the gNB, CU or CU-CP, or None, why there is none) of a run."""
    if cu_arg:
        return Path(cu_arg).resolve(), ""
    if path_str is None:
        # Only explicit logs: a gNB log also writes NGAP.
        if gnb_log is not None:
            return gnb_log, ""
        return None, "no run dir and no --gnb or --cu"
    logs = correlate_radio.ocudu_logs(resolve.build_inventory(path_str), NGAP_ROLES)
    if not logs:
        return None, "no gNB, CU or CU-CP component in the run (pass --cu)"
    return min(logs, key=lambda x: x[0])[1], ""


def ngap_pcap_beside(log: Path):
    """NGAP pcap of the component of log, e.g. ngap.pcap or gnb_ngap.pcap, or None."""
    return next(iter(sorted(log.parent.glob("*ngap.pcap"))), None)


def first_common_pusch(gnb_log, ue_log, ref_date):
    """Find the earliest PUSCH present in both logs at the same (slot, rnti)."""
    g = correlate_radio.parse_gnb_phy(gnb_log, "PUSCH", None)
    u = correlate_radio.parse_ue_phy(ue_log, "PUSCH", None, ref_date)
    best = None
    for key in set(g) & set(u):
        pairs, _, _ = correlate_radio._pair_by_time(g[key], u[key])
        for gp, up in pairs:
            if gp["dt"] is None or up["dt"] is None:
                continue
            if best is None or gp["dt"] < best[0]["dt"]:
                best = (gp, up, key)
    return best


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?",
                    help="test dir or run dir (default: the current dir, unless --gnb, --ue or --cu)")
    ap.add_argument("--gnb", help="explicit gNB or DU log with PHY lines")
    ap.add_argument("--ue", help="explicit ue.log path")
    ap.add_argument("--cu", help="explicit gNB, CU or CU-CP log for the log <-> pcap check")
    ap.add_argument("--pcap", help="an NGAP pcap (defaults to the one beside the gNB, CU or CU-CP log)")
    args = ap.parse_args(argv)

    path = args.path
    if path is None and not (args.gnb or args.ue or args.cu):
        path = "."
    gnb_log, ue_log, _ = correlate_radio.resolve_logs(path, args.gnb, args.ue)
    ngap_log, ngap_why = resolve_ngap_log(path, args.cu, gnb_log)
    pcap = Path(args.pcap).resolve() if args.pcap else None
    if pcap is None and ngap_log is not None:
        pcap = ngap_pcap_beside(ngap_log)
        if pcap is None:
            ngap_why = f"no *ngap.pcap beside {ngap_log} (pass --pcap)"
    if pcap is not None and ngap_log is None:
        ngap_why = "no gNB, CU or CU-CP log for the pcap (pass --cu)"

    print(f"PHY log  : {gnb_log}")
    print(f"UE  log  : {ue_log}")
    print(f"NGAP log : {ngap_log}")
    print(f"NGAP pcap: {pcap}")
    print()

    ref_date = None
    if gnb_log:
        first = utils.first_gnb_event_ts(gnb_log)
        ref_date = first.split("T")[0] if first else None

    # 1. log <-> pcap
    if ngap_log and pcap:
        g_ts = gnb_ngsetup_ts(ngap_log)
        p_epoch = first_ngap_frame_epoch(pcap)
        if g_ts and p_epoch is not None:
            g_dt = utils.parse_gnb_ts(g_ts)
            p_dt = utils.epoch_to_utc(p_epoch)
            delta_ms = (p_dt - g_dt).total_seconds() * 1000
            verdict = "share UTC ✓" if abs(delta_ms) < 1000 else "OFFSET — investigate"
            w = max(len(ngap_log.name), len("pcap"))
            print("[log <-> pcap]  NGSetupRequest")
            print(f"   {ngap_log.name:<{w}} : {g_ts}")
            print(f"   {'pcap':<{w}} : {utils.epoch_to_utc(p_epoch).isoformat()}  (raw epoch {p_epoch})")
            print(f"   Δ = {delta_ms:+.1f} ms  -> {ngap_log.name} and pcap {verdict}")
        elif g_ts is None:
            print(f"[log <-> pcap]  skipped: NGSetupRequest not in {ngap_log.name} (a DU log, NGAP logging below "
                  "info, or the log starts after NG setup).")
            print("   The log and pcap are written by the same process, so they share UTC regardless;")
            print("   the UE<->gNB check below still anchors the radio timeline.")
        else:
            print("[log <-> pcap]  could not read the pcap epoch (tshark/AppArmor — "
                  "the pcap is staged to /tmp; check tshark availability).")
    else:
        print(f"[log <-> pcap]  skipped: {ngap_why}")
    print()

    # 2. UE <-> gNB PHY
    if gnb_log and ue_log:
        best = first_common_pusch(gnb_log, ue_log, ref_date)
        if best:
            gp, up, key = best
            slot, rnti = key
            w = max(len(ue_log.name), len(gnb_log.name))
            print("[UE <-> gNB]  first co-identified PUSCH")
            print(f"   slot {slot}  rnti {correlate_radio.shown_rnti(rnti)}")
            print(f"   {ue_log.name:<{w}} : {up['ts']}")
            print(f"   {gnb_log.name:<{w}} : {gp['ts']}")
            if gp["dt"] and up["dt"]:
                d_ms = (gp["dt"] - up["dt"]).total_seconds() * 1000
                print(f"   Δ = {d_ms:+.1f} ms  (gNB decode-log latency; SFN.slot identical -> "
                      f"no PHY SFN offset ✓)")
        elif not correlate_radio.has_phy(gnb_log):
            print(f"[UE <-> gNB]  no PHY lines in {gnb_log.name} (a CU log, or phy_level below info); "
                  "pass a gNB or DU log with --gnb")
        else:
            print("[UE <-> gNB]  (no co-identified PUSCH found)")
    else:
        print(f"[UE <-> gNB]  skipped: no {'gNB or DU log (--gnb)' if gnb_log is None else 'UE log (--ue)'}")
    print()

    # 3. slot_rx=
    if gnb_log:
        has_slot_rx = False
        with open(gnb_log, errors="replace") as f:
            for line in f:
                if "slot_rx=" in line:
                    has_slot_rx = True
                    break
        print(f"[slot_rx=]  present in {gnb_log.name}: {'yes' if has_slot_rx else 'no'}"
              + ("" if has_slot_rx else "  (MAC<->PHY: join on (SFN.slot,RNTI) / calibrate delay)"))
        print()

    print(f"Conclusion: {ngap_log.name if ngap_log else 'the gNB or CU log'} and the pcaps its process writes share "
          "one clock")
    print("by construction. Correlate radio events on (SFN.slot, RNTI); use raw")
    print("frame.time_epoch for pcaps (NOT capinfos local-TZ display). Off-host sources")
    print("(VIAVI tester, remote 5GC, a UE sim on another box) are NOT checked here and may")
    print("be offset — measure their offset from a matched RNTI/event before comparing wall-clocks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

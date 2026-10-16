#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
correlate_radio.py — Cross-correlate PHY radio events between the Amarisoft UE
log and the OCUDU gNB log for one run.

The reliable join is the PHY layer key **(SFN.slot, RNTI)**: the UE PHY and gNB
PHY log the same PUSCH/PUCCH/PDCCH/PDSCH at the same slot and RNTI. The SFN.slot
string recurs every 1024 frames (~10.24 s), so same-(slot,rnti) events are
disambiguated by nearest wall-clock within MATCH_TOL.

UL kinds (PUSCH/PUCCH/PRACH — UE transmits, gNB receives):
  - rx-ok                  : gNB decoded it (crc=OK), UE transmitted it
  - rx-ko/ue-tx            : gNB crc=KO but the UE *did* transmit -> real decode/
                             channel issue or ZMQ sample misalignment (NOT a DTX)
  - rx-ko/ue-silent        : gNB crc=KO, sinr=inf, UE logged no TX -> DTX
  - rx-ko/ue-missing       : gNB crc=KO, finite sinr, no UE TX matched
  - ue-missing-tx          : gNB decoded a transmission (crc=OK) the UE has no
                             record of sending -> suspect the join, the ref-date,
                             or a truncated ue.log; re-check before concluding
  - gnb-missing            : UE transmitted but no gNB event near it in time
  - ue-extra-tx/contention : a 2nd UE TX on a (slot,rnti) the gNB already paired
                             (RACH contention; expected, not an anomaly)
DL kinds (PDSCH/PDCCH — gNB transmits, UE receives):
  - tx-rx-ok               : both sides logged it
  - ue-missing-rx          : gNB transmitted but the UE logged no reception
  - gnb-missing-tx         : UE received something the gNB PHY has no TX for

Caveats:
  - **PUCCH has no `crc=` field**, so PUCCH never yields the rx-ko/* (DTX)
    classes — paired PUCCH is always rx-ok; a `sinr=-inf` PUCCH occasion is a
    normal no-detection, not flagged.
  - **PRACH**: per-side indices differ (UE sequence_index != gNB idx), so PRACH
    is correlated loosely by wall-clock occasion (see --kind prach), not joined
    on (slot,rnti).

This folds in the old ue_rlf_trace.py DTX-vs-degradation classification, now
cross-checked against the UE side.

Usage:
    python3 correlate_radio.py <test-or-run-dir> [--gnb PATH] [--ue PATH]
        [--kind pusch|pucch|pdsch|pdcch|prach] [--rnti 4601|0x4601|17921]
        [--only anomalies|all] [--max-rows 200]
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

import utils
import resolve

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))

from parsers.ran.rnti import normalize, parse_any  # noqa: E402

# SFN wraps every 1024 frames (~10.24 s), so an "SFN.slot" string recurs over a
# run. Sources share UTC, so we disambiguate same-(slot,rnti) events by pairing
# the nearest in wall-clock within this tolerance (well below one wrap, well
# above the ~10 ms gNB decode-log latency).
MATCH_TOL = timedelta(seconds=0.5)


# --------------------------------------------------------------------------
# Locate the gnb.log and ue.log for a run/test dir (or take them explicitly)
# --------------------------------------------------------------------------

def resolve_logs(path_str, gnb_arg, ue_arg):
    gnb = Path(gnb_arg) if gnb_arg else None
    ue = Path(ue_arg) if ue_arg else None
    if gnb and ue:
        return gnb, ue
    inv = resolve.build_inventory(path_str)
    for c in inv["components"]:
        rd = Path(c["run_dir"])
        if gnb is None and c["role"] in ("gnb", "du", "cu", "cu-cp", "cu-up", "odu", "ocu"):
            for n in resolve.OCUDU_LOG_NAMES:
                if (rd / n).is_file():
                    gnb = rd / n
                    break
        if ue is None and c["role"] == "ue" and (rd / "ue.log").is_file():
            ue = rd / "ue.log"
    return gnb, ue


# --------------------------------------------------------------------------
# Parse PHY events into {(slot, rnti): event} (UL/DL kinds carry an RNTI).
# --------------------------------------------------------------------------

UL_KINDS = {"PUSCH", "PUCCH", "PRACH"}


def parse_gnb_phy(gnb_log, kind, rnti_filter):
    """Return {(slot, rnti): [event, ...]} for the given PHY kind."""
    events = defaultdict(list)
    with open(gnb_log, errors="replace") as f:
        for line in f:
            if line[:1] != "2":
                continue
            m = utils.GNB_PHY_RE.match(line)
            if not m or m.group("kind") != kind:
                continue
            rest = m.group("rest")
            rm = utils.GNB_RNTI_RE.search(rest)
            rnti = normalize(rm.group("rnti")) if rm else None
            if rnti_filter and rnti != rnti_filter:
                continue
            crc = utils.GNB_CRC_RE.search(rest)
            sinr = utils.GNB_SINR_RE.search(rest)
            events[(m.group("slot"), rnti)].append({
                "ts": m.group("ts"),
                "dt": utils.parse_gnb_ts(m.group("ts")),
                "slot": m.group("slot"),
                "rnti": rnti,
                "crc": crc.group("crc") if crc else None,
                "sinr": sinr.group("sinr") if sinr else None,
            })
    return events


def parse_ue_phy(ue_log, kind, rnti_filter, ref_date):
    """Return {(slot, rnti): [event, ...]} for the given PHY kind."""
    events = defaultdict(list)
    for line in open(ue_log, errors="replace"):
        if line[:1] not in "0123456789":
            continue
        m = utils.UE_PHY_RE.match(line)
        if not m or m.group("kind") != kind:
            continue
        rnti = normalize(m.group("rnti"), bare_hex=True)
        if rnti_filter and rnti != rnti_filter:
            continue
        try:
            dt = utils.parse_hms_ts(m.group("ts"), ref_date) if ref_date else None
        except ValueError:
            dt = None
        events[(m.group("slot"), rnti)].append({
            "ts": m.group("ts"),
            "dt": dt,
            "slot": m.group("slot"),
            "rnti": rnti,
            "ueid": m.group("ueid"),
        })
    return events


def _pair_by_time(glist, ulist):
    """Greedily pair gNB/UE events sharing a (slot,rnti) by nearest wall-clock
    within MATCH_TOL. Returns (pairs, gnb_only, ue_only). Falls back to
    order-based pairing when timestamps are unavailable."""
    gl = sorted(glist, key=lambda e: (e["dt"] is None, e["dt"] or 0))
    ul = sorted(ulist, key=lambda e: (e["dt"] is None, e["dt"] or 0))
    pairs, g_only, u_only = [], [], []
    gi = ui = 0
    while gi < len(gl) and ui < len(ul):
        g, u = gl[gi], ul[ui]
        if g["dt"] is None or u["dt"] is None:
            pairs.append((g, u)); gi += 1; ui += 1; continue
        delta = g["dt"] - u["dt"]
        if abs(delta) <= MATCH_TOL:
            pairs.append((g, u)); gi += 1; ui += 1
        elif delta < timedelta(0):       # gNB event earlier than UE -> unmatched gNB
            g_only.append(g); gi += 1
        else:                            # UE event earlier than gNB -> unmatched UE
            u_only.append(u); ui += 1
    g_only.extend(gl[gi:])
    u_only.extend(ul[ui:])
    return pairs, g_only, u_only


def classify_ko(gnb_ev, ue_ev) -> str:
    sinr = (gnb_ev.get("sinr") or "")
    silent = "inf" in sinr.lower()
    if ue_ev is not None:
        return "rx-ko/ue-tx"
    return "rx-ko/ue-silent" if silent else "rx-ko/ue-missing"


# --------------------------------------------------------------------------
# Correlation
# --------------------------------------------------------------------------

def correlate(gnb_log, ue_log, kind, rnti_filter, ref_date):
    kind_u = kind.upper()
    is_ul = kind_u in UL_KINDS
    gnb_ev = parse_gnb_phy(gnb_log, kind_u, rnti_filter) if gnb_log else {}
    ue_ev = parse_ue_phy(ue_log, kind_u, rnti_filter, ref_date) if ue_log else {}

    rows = []
    counts = Counter()
    keys = set(gnb_ev) | set(ue_ev)
    for key in sorted(keys, key=lambda k: (utils.slot_key(k[0]) or (0, 0), str(k[1]))):
        slot, rnti = key
        pairs, g_only, u_only = _pair_by_time(gnb_ev.get(key, []), ue_ev.get(key, []))

        def emit(status, g, u):
            counts[status] += 1
            rows.append({
                "slot": slot, "rnti": rnti, "status": status,
                "gnb_ts": g["ts"] if g else None,
                "ue_ts": u["ts"] if u else None,
                "crc": g.get("crc") if g else None,
                "sinr": g.get("sinr") if g else None,
                "ueid": u.get("ueid") if u else None,
            })

        for g, u in pairs:
            if is_ul:
                status = "rx-ok" if (g.get("crc") in ("OK", None)) else classify_ko(g, u)
            else:
                status = "tx-rx-ok"
            emit(status, g, u)
        for g in g_only:
            if is_ul:
                # gNB event with no matching UE TX in window
                status = "ue-missing-tx" if g.get("crc") in ("OK", None) else classify_ko(g, None)
            else:
                # DL: gNB transmitted but the UE logged no reception -> UE missed it.
                status = "ue-missing-rx"
            emit(status, g, None)
        gnb_bucket = gnb_ev.get(key, [])
        tol = MATCH_TOL.total_seconds()
        for u in u_only:
            if not is_ul:
                # DL: UE received something the gNB PHY has no TX for.
                status = "gnb-missing-tx"
            elif u["dt"] is not None and any(
                g["dt"] is not None and abs((g["dt"] - u["dt"]).total_seconds()) <= tol
                for g in gnb_bucket
            ):
                # A gNB event exists at this (slot,rnti) within the same time window
                # (it paired to another UE TX) -> a second UE transmitted on the same
                # grant: RACH contention, not a loss.
                status = "ue-extra-tx/contention"
            else:
                # No gNB event near this UE TX in time -> a genuine gNB-side miss,
                # even if the slot string recurs in another superframe.
                status = "gnb-missing"
            emit(status, None, u)
    return rows, counts


# Statuses worth surfacing first. "ue-extra-tx/contention" is expected during
# multi-UE RACH and is NOT listed here (it's normal contention, not a fault).
ANOMALY_STATUSES = {
    "gnb-missing", "rx-ko/ue-tx", "rx-ko/ue-silent", "rx-ko/ue-missing",
    "ue-missing-tx", "ue-missing-rx",
}


def prach_summary(gnb_log, ue_log):
    """PRACH is correlated loosely: per-side indices differ, so just count and
    flag UE PRACH occasions with no gNB detection in a nearby wall-clock window."""
    ue_tx = []
    for line in open(ue_log, errors="replace") if ue_log else []:
        m = utils.UE_PHY_RE.match(line)
        if m and m.group("kind") == "PRACH":
            ue_tx.append((m.group("slot"), m.group("ueid"), m.group("ts")))
    gnb_det = 0
    if gnb_log:
        for line in open(gnb_log, errors="replace"):
            mm = utils.GNB_PHY_RE.match(line)
            if mm and mm.group("kind") == "PRACH":
                gnb_det += len(utils.GNB_PREAMBLE_RE.findall(mm.group("rest")))
    return len(ue_tx), gnb_det, ue_tx


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", default=".", help="test dir or run dir")
    ap.add_argument("--gnb", help="explicit gnb.log path")
    ap.add_argument("--ue", help="explicit ue.log path")
    ap.add_argument("--kind", default="pusch",
                    choices=["pusch", "pucch", "pdsch", "pdcch", "prach"])
    ap.add_argument("--rnti", help="restrict to one RNTI, in any form: 4601 (hex as logs print it), 0x4601, "
                                   "or 17921 (decimal as F1AP pcaps hold it)")
    ap.add_argument("--only", default="anomalies", choices=["anomalies", "all"])
    ap.add_argument("--max-rows", type=int, default=200)
    args = ap.parse_args(argv)

    rnti_filter = parse_any(args.rnti) if args.rnti else None
    if args.rnti and rnti_filter is None:
        ap.error(f"invalid RNTI {args.rnti!r}, expected 4601, 0x4601 or 17921")

    gnb_log, ue_log = resolve_logs(args.path, args.gnb, args.ue)
    if gnb_log is None and ue_log is None:
        print("error: could not locate gnb.log or ue.log", file=sys.stderr)
        return 1
    print(f"gNB log : {gnb_log}")
    print(f"UE  log : {ue_log}")

    # Date anchor for the UE log's HH:MM:SS clock (UE/gNB share UTC). Prefer the
    # gNB first-event date; fall back to the UE log's `# Started on` header.
    ref_date = None
    if gnb_log:
        first = utils.first_gnb_event_ts(gnb_log)
        ref_date = first.split("T")[0] if first else None
    if ref_date is None and ue_log:
        ref_date = utils.started_on_date(ue_log)

    if args.kind == "prach":
        n_ue, n_gnb, _ = prach_summary(gnb_log, ue_log)
        print(f"\nPRACH (loose correlation — UE sequence_index != gNB idx):")
        print(f"  UE PRACH transmissions : {n_ue}")
        print(f"  gNB detected preambles : {n_gnb}")
        print("  Note: match PRACH attempts to the gNB via the resulting tc-rnti and the")
        print("  subsequent Msg3 PUSCH (exact (SFN.slot, RNTI) join), not the raw index.")
        return 0

    rows, counts = correlate(gnb_log, ue_log, args.kind, rnti_filter, ref_date)
    print(f"\n{args.kind.upper()} correlation on (SFN.slot, RNTI):")
    total = sum(counts.values())
    print(f"  total correlated events: {total}")
    for status, n in counts.most_common():
        print(f"    {n:6d}  {status}")

    shown = [r for r in rows if (args.only == "all" or r["status"] in ANOMALY_STATUSES)]
    if not shown:
        print("\n  No anomalies." if args.only == "anomalies" else "\n  (no rows)")
        return 0
    print(f"\n  {'slot':>9}  {'rnti':<8} {'status':<18} {'crc':<3} {'sinr':<8} ue_ts / gnb_ts")
    for r in shown[: args.max_rows]:
        print(f"  {r['slot']:>9}  {str(r['rnti']):<8} {r['status']:<18} "
              f"{str(r['crc'] or '-'):<3} {str(r['sinr'] or '-'):<8} "
              f"{r['ue_ts'] or '-'} / {r['gnb_ts'] or '-'}")
    if len(shown) > args.max_rows:
        print(f"  ... {len(shown) - args.max_rows} more rows (raise --max-rows or add --rnti)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

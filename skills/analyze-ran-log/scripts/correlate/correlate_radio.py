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
                             or a truncated ue.log; re-check before concluding.
                             For PUCCH, a false detection at low sinr (e.g.
                             sr=yes); ignore it.
  - ue-missed-dl           : gNB PUCCH with a HARQ-ACK field, sinr=-inf or no
                             valid UCI (ack=2..), and no UE TX -> a PDSCH was
                             scheduled but the UE missed its PDCCH (DL DTX),
                             or no longer uses the RNTI (check ue.log for it)
  - no-detection           : other gNB PUCCH with sinr=-inf or no valid UCI
                             (sr=no) and no UE TX -> an SR or CSI occasion the
                             UE did not use; not an anomaly
  - gnb-missing            : UE transmitted but no gNB event near it in time
  - ue-extra-tx/contention : a 2nd UE TX on a (slot,rnti) the gNB already paired
                             (RACH contention; expected, not an anomaly)
  - gnb-extra-rx           : PUCCH only: another resource the gNB decoded at a
                             (slot,rnti) whose UE TX paired with its best one;
                             not an anomaly
DL kinds (PDSCH/PDCCH — gNB transmits, UE receives):
  - tx-rx-ok               : both sides logged it
  - ue-missing-rx          : gNB transmitted but the UE logged no reception
  - gnb-missing-tx         : UE received something the gNB PHY has no TX for

Caveats:
  - **PUCCH has no `crc=` field**, so PUCCH never yields the rx-ko/* (DTX)
    classes — paired PUCCH is always rx-ok. A gNB PUCCH shows no UE TX when
    `sinr=-inf` or no field holds valid UCI (`sr=no`, payloads of 2s such as
    `ack=2` or `csi1=2222`); one valid field, e.g. `ack=11 csi1=2222`, is a TX.
  - The UE logs RNTI 0000 for the Msg4 HARQ-ACK PUCCH. Such a UE event takes
    the RNTI of the UE id's last UL TX (the Msg3 TC-RNTI); with none, it pairs
    with a detected gNB event of any RNTI at its slot.
  - Both logs are needed; with one side only the script exits 1.
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

# OCUDU roles in the order their logs are picked, those with a PHY first.
OCUDU_ROLES = ("gnb", "du", "odu", "cu", "cu-cp", "cu-up", "ocu")


def ocudu_logs(inv, roles):
    """[(index of the role in roles, primary log)] of the OCUDU components of the inventory with one of roles."""
    found = []
    for c in inv["components"]:
        rd = Path(c["run_dir"])
        if c["role"] in roles:
            log = next((rd / n for n in resolve.OCUDU_LOG_NAMES if (rd / n).is_file()), None)
            if log:
                found.append((roles.index(c["role"]), log))
    return found


def resolve_logs(path_str, gnb_arg, ue_arg):
    """(gNB log, UE log, other OCUDU logs of the role of the gNB log) of a run, each None or empty when absent.

    path_str None means no run dir: only the explicit logs are taken.
    """
    gnb = Path(gnb_arg).resolve() if gnb_arg else None
    ue = Path(ue_arg).resolve() if ue_arg else None
    if (gnb and ue) or path_str is None:
        return gnb, ue, []
    inv = resolve.build_inventory(path_str)
    if ue is None:
        ue = next((Path(c["run_dir"]) / "ue.log" for c in inv["components"]
                   if c["role"] == "ue" and (Path(c["run_dir"]) / "ue.log").is_file()), None)
    ocudu = ocudu_logs(inv, OCUDU_ROLES)
    others = []
    if gnb is None and ocudu:
        best = min(rank for rank, _ in ocudu)
        same = [log for rank, log in ocudu if rank == best]
        gnb, others = same[0], same[1:]
    return gnb, ue, others


def shown_rnti(rnti) -> str:
    """RNTI as gNB PHY lines print it, without padding, e.g. 0x44, from its normalized form."""
    return f"0x{int(rnti, 16):x}" if rnti else "-"


def has_phy(gnb_log) -> bool:
    """Whether a gNB or DU log has a PHY radio event line."""
    with open(gnb_log, errors="replace") as f:
        return any("[PHY" in line[:40] and utils.GNB_PHY_RE.match(line) for line in f)


def ue_has_phy(ue_log) -> bool:
    """Whether a UE log has a PHY radio event line."""
    with open(ue_log, errors="replace") as f:
        return any("[PHY]" in line[:20] and utils.UE_PHY_RE.match(line) for line in f)


def ue_clock_ref(gnb_log, ue_log):
    """Date anchor of the HH:MM:SS clock of ue_log, see utils.HmsClock, or None.

    UE and gNB share UTC, so the first gNB event anchors it; else the `# Started on` header of the UE log.
    """
    first = utils.first_gnb_event_ts(gnb_log)
    return utils.parse_gnb_ts(first) if first else utils.started_on_date(ue_log)


# --------------------------------------------------------------------------
# Parse PHY events into {(slot, rnti): event} (UL/DL kinds carry an RNTI).
# --------------------------------------------------------------------------

UL_KINDS = {"PUSCH", "PUCCH", "PRACH"}
# RNTI of UE events logged before the UE adopts its C-RNTI, e.g. the Msg4 HARQ-ACK PUCCH.
UNKNOWN_RNTI = "0x0000"


def not_transmitted(kind, rest, sinr) -> tuple[bool, bool]:
    """(UE sent nothing, UE missed a DL assignment) of a gNB PUCCH.

    The UE sent nothing when there is no signal or no field holds valid UCI; it then missed a DL assignment when the
    PUCCH has a HARQ-ACK field.
    """
    if kind != "PUCCH":
        return False, False
    fields = {m.group("field"): m.group("value") for m in utils.GNB_PUCCH_UCI_RE.finditer(rest)}
    valid = any(v != "no" and set(v) != {"2"} for v in fields.values())
    no_tx = "inf" in sinr.lower() or (bool(fields) and not valid)
    return no_tx, no_tx and "ack" in fields


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
            no_tx, missed_dl = not_transmitted(kind, rest, sinr.group("sinr") if sinr else "")
            events[(m.group("slot"), rnti)].append({
                "ts": m.group("ts"),
                "dt": utils.parse_gnb_ts(m.group("ts")),
                "slot": m.group("slot"),
                "rnti": rnti,
                "crc": crc.group("crc") if crc else None,
                "sinr": sinr.group("sinr") if sinr else None,
                "no_tx": no_tx,
                "missed_dl": missed_dl,
            })
    return events


def ue_phy_events(ue_log):
    """Yield (match of utils.UE_PHY_RE, RNTI) of the UE PHY lines of ue_log.

    An event of unknown RNTI takes the RNTI of the last UL transmission of its UE id, else stays unknown.
    """
    # Last UL RNTI of each UE id.
    last_ul = {}
    with open(ue_log, errors="replace") as f:
        for line in f:
            if line[:1] not in "0123456789":
                continue
            m = utils.UE_PHY_RE.match(line)
            if not m:
                continue
            rnti = normalize(m.group("rnti"), bare_hex=True)
            if rnti == UNKNOWN_RNTI:
                # The Msg4 HARQ-ACK follows the Msg3 PUSCH on the TC-RNTI that becomes the C-RNTI. A later RNTI of
                # the UE id may belong to a new random access.
                rnti = last_ul.get(m.group("ueid"), UNKNOWN_RNTI)
            elif rnti and m.group("dir") == "UL":
                last_ul[m.group("ueid")] = rnti
            yield m, rnti


def parse_ue_phy(ue_log, kind, rnti_filter, ref):
    """Return {(slot, rnti): [event, ...]} for the given PHY kind.

    ref is the date anchor of the UE clock, see utils.HmsClock, or None for events without a datetime.
    """
    events = defaultdict(list)
    clock = utils.HmsClock(ref) if ref else None
    for m, rnti in ue_phy_events(ue_log):
        # Every PHY line advances the clock, so a midnight crossing is seen whatever the kind kept.
        try:
            dt = clock.parse(m.group("ts")) if clock else None
        except ValueError:
            dt = None
        if m.group("kind") != kind or (rnti_filter and rnti != rnti_filter):
            continue
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


def near(a, b) -> bool:
    """Whether events a and b are within MATCH_TOL in wall-clock, or either has no timestamp."""
    return a["dt"] is None or b["dt"] is None or abs(a["dt"] - b["dt"]) <= MATCH_TOL


def no_signal(gnb_ev) -> bool:
    """Whether the gNB measured no signal (sinr=inf or -inf) on the event."""
    return "inf" in (gnb_ev.get("sinr") or "").lower()


def classify_ko(gnb_ev, ue_ev) -> str:
    if ue_ev is not None:
        return "rx-ko/ue-tx"
    return "rx-ko/ue-silent" if no_signal(gnb_ev) else "rx-ko/ue-missing"


def sinr_db(gnb_ev) -> float:
    """SINR of a gNB event in dB, -inf when absent or not a number."""
    try:
        return float((gnb_ev.get("sinr") or "").removesuffix("dB"))
    except ValueError:
        return float("-inf")


def split_pucch_resources(pairs, g_only):
    """(pairs, gNB events left, extra gNB events) of a PUCCH (slot, rnti).

    The gNB may decode several PUCCH resources of a UE in a slot while the UE uses one, so each UE TX takes the best
    gNB event in its window and the others in that window are extra.
    """
    extra = []
    for i, (g, u) in enumerate(pairs):
        near = [e for e in g_only if e["dt"] is None or u["dt"] is None or abs(e["dt"] - u["dt"]) <= MATCH_TOL]
        best = min([g] + near, key=lambda e: (e["no_tx"], -sinr_db(e)))
        pairs[i] = (best, u)
        g_only = [e for e in g_only if all(e is not n for n in near)]
        extra += [e for e in near + [g] if e is not best]
    return pairs, g_only, extra


def take_unknown_rnti(unknown, g):
    """Pop the UE event of unknown RNTI nearest in wall-clock to gNB event g within MATCH_TOL, or None.

    A gNB event that shows no UE TX takes none.
    """
    if g["no_tx"]:
        return None
    cands = [u for u in unknown.get(g["slot"], []) if near(g, u)]
    if not cands:
        return None
    u = min(cands, key=lambda e: abs(g["dt"] - e["dt"]) if g["dt"] and e["dt"] else timedelta(0))
    unknown[g["slot"]].remove(u)
    return u


# --------------------------------------------------------------------------
# Correlation
# --------------------------------------------------------------------------

def correlate(gnb_log, ue_log, kind, rnti_filter, ref):
    kind_u = kind.upper()
    is_ul = kind_u in UL_KINDS
    gnb_ev = parse_gnb_phy(gnb_log, kind_u, rnti_filter)
    ue_ev = parse_ue_phy(ue_log, kind_u, rnti_filter, ref)
    # UE events of unknown RNTI by slot, joined to unpaired gNB events of any RNTI that show a UE TX.
    unknown = defaultdict(list)
    for key in [k for k in ue_ev if k[1] == UNKNOWN_RNTI]:
        unknown[key[0]] += ue_ev.pop(key)

    rows = []
    counts = Counter()

    def emit(status, slot, rnti, g, u):
        counts[status] += 1
        rows.append({
            "slot": slot, "rnti": rnti, "status": status,
            "gnb_ts": g["ts"] if g else None,
            "ue_ts": u["ts"] if u else None,
            "crc": g.get("crc") if g else None,
            "sinr": g.get("sinr") if g else None,
            "ueid": u.get("ueid") if u else None,
        })

    keys = sorted(set(gnb_ev) | set(ue_ev), key=lambda k: (utils.slot_key(k[0]) or (0, 0), str(k[1])))
    joined = {key: _pair_by_time(gnb_ev.get(key, []), ue_ev.get(key, [])) for key in keys}
    if is_ul and unknown:
        for key in keys:
            pairs, g_only, _ = joined[key]
            for g in list(g_only):
                u = take_unknown_rnti(unknown, g)
                if u is not None:
                    pairs.append((g, u))
                    g_only.remove(g)

    for key in keys:
        slot, rnti = key
        pairs, g_only, u_only = joined[key]
        if kind_u == "PUCCH":
            pairs, g_only, extra = split_pucch_resources(pairs, g_only)
            for g in extra:
                emit("gnb-extra-rx", slot, rnti, g, None)
        for g, u in pairs:
            if is_ul:
                status = "rx-ok" if (g.get("crc") in ("OK", None)) else classify_ko(g, u)
            else:
                status = "tx-rx-ok"
            emit(status, slot, rnti, g, u)
        # gNB missed-DL events of the (slot, rnti), one per HARQ-ACK occasion.
        missed = []
        for g in g_only:
            if not is_ul:
                # DL: gNB transmitted but the UE logged no reception -> UE missed it.
                status = "ue-missing-rx"
            elif g.get("crc") == "KO":
                status = classify_ko(g, None)
            elif g["missed_dl"] and not any(near(g, h) for h in missed):
                # A HARQ-ACK occasion follows a PDSCH, so the UE missed its PDCCH: a DL DTX. The gNB may check
                # several resources of the occasion, so the others read no-detection.
                missed.append(g)
                status = "ue-missed-dl"
            elif g["no_tx"]:
                # A PUCCH occasion, e.g. for SR, the UE did not use.
                status = "no-detection"
            else:
                status = "ue-missing-tx"
            emit(status, slot, rnti, g, None)
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
            emit(status, slot, rnti, None, u)
    for slot, ues in unknown.items():
        for u in ues:
            emit("gnb-missing", slot, UNKNOWN_RNTI, None, u)
    return rows, counts


# Statuses worth surfacing first. "ue-extra-tx/contention" is expected during
# multi-UE RACH and is NOT listed here (it's normal contention, not a fault).
ANOMALY_STATUSES = {
    "gnb-missing", "rx-ko/ue-tx", "rx-ko/ue-silent", "rx-ko/ue-missing",
    "ue-missing-tx", "ue-missing-rx", "ue-missed-dl",
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
    ap.add_argument("path", nargs="?", help="test dir or run dir (default: the current dir, unless --gnb or --ue)")
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

    path = args.path if args.path or args.gnb or args.ue else "."
    gnb_log, ue_log, others = resolve_logs(path, args.gnb, args.ue)
    if gnb_log is None or ue_log is None:
        missing = "gNB or DU log (--gnb)" if gnb_log is None else "UE log (--ue)"
        print(f"error: no {missing}; nothing to correlate", file=sys.stderr)
        return 1
    if not has_phy(gnb_log):
        print(f"error: no PHY lines in {gnb_log} (a CU log, or phy_level below info); pass a gNB or DU log with --gnb",
              file=sys.stderr)
        return 1
    if not ue_has_phy(ue_log):
        print(f"error: no PHY lines in {ue_log} (empty, or PHY logging off in its log_options); pass a UE log "
              "with PHY lines with --ue", file=sys.stderr)
        return 1
    print(f"gNB log : {gnb_log}")
    for o in others:
        print(f"  other : {o} (UE events on its cells read gnb-missing; rerun with --gnb)")
    print(f"UE  log : {ue_log}")

    ref = ue_clock_ref(gnb_log, ue_log)

    if args.kind == "prach":
        n_ue, n_gnb, _ = prach_summary(gnb_log, ue_log)
        print(f"\nPRACH (loose correlation — UE sequence_index != gNB idx):")
        print(f"  UE PRACH transmissions : {n_ue}")
        print(f"  gNB detected preambles : {n_gnb}")
        print("  Note: match PRACH attempts to the gNB via the resulting tc-rnti and the")
        print("  subsequent Msg3 PUSCH (exact (SFN.slot, RNTI) join), not the raw index.")
        return 0

    rows, counts = correlate(gnb_log, ue_log, args.kind, rnti_filter, ref)
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
        print(f"  {r['slot']:>9}  {shown_rnti(r['rnti']):<8} {r['status']:<18} "
              f"{str(r['crc'] or '-'):<3} {str(r['sinr'] or '-'):<8} "
              f"{r['ue_ts'] or '-'} / {r['gnb_ts'] or '-'}")
    if len(shown) > args.max_rows:
        print(f"  ... {len(shown) - args.max_rows} more rows (raise --max-rows or add --rnti)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

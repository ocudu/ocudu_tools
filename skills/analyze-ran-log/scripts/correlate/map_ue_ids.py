#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
Extract UE ID mappings from F1AP, NGAP, or E1AP pcap files.

Runs tshark on the given pcap and tracks how UE identifiers map to each other,
printing one line per mapping update or release:

    <frame>, <timestamp>, <message>, <id1>=<val1>, <id2>=<val2>, ...

Release lines are suffixed with [released].

For F1AP: tracks du_ue, cu_ue, c_rnti (printed in hex).
For NGAP: tracks ran_ue, amf_ue.
For E1AP: tracks cu_cp_ue, cu_up_ue.

Usage:
    python3 map_ue_ids.py <pcap_file>

The protocol is auto-detected from the filename (ngap/f1ap/e1ap) or by
probing the first packets with tshark.
"""

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

import utils


def _stage_for_tshark(pcap_file):
    """Copy the pcap under the shared /tmp cache if outside /tmp (Ubuntu AppArmor
    restricts tshark reads to /tmp). Pass through if already under /tmp."""
    # A symlink under /tmp to a file elsewhere is still denied, so its target is checked.
    src = os.path.realpath(pcap_file)
    if src.startswith("/tmp/"):
        return src
    dst = utils.cache_path(src, "stage", suffix="pcap")
    if not dst.exists():
        try:
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)
    return str(dst)


# (label, tshark_field, output_as_hex)
_ID_FIELDS = {
    'f1ap': [
        ('du_ue',   'f1ap.GNB_DU_UE_F1AP_ID', False),
        ('cu_ue',   'f1ap.GNB_CU_UE_F1AP_ID', False),
        ('c_rnti',  'f1ap.C_RNTI',             True),
    ],
    'ngap': [
        ('ran_ue',  'ngap.RAN_UE_NGAP_ID',      False),
        ('amf_ue',  'ngap.AMF_UE_NGAP_ID',      False),
    ],
    'e1ap': [
        ('cu_cp_ue', 'e1ap.GNB_CU_CP_UE_E1AP_ID', False),
        ('cu_up_ue', 'e1ap.GNB_CU_UP_UE_E1AP_ID', False),
    ],
}

# These messages always introduce a new UE context — never merge with an
# existing record even if a same ID is recycled from a released UE.
_NEW_UE_MSGS = {
    'f1ap': {'InitialULRRCMessageTransfer'},
    'ngap': {'InitialUEMessage'},
    'e1ap': {'BearerContextSetupRequest'},
}

# These messages signal that a UE context has been fully released.
# The matching record is removed so its IDs can be safely recycled.
_RELEASE_MSGS = {
    'f1ap': {'UEContextReleaseComplete'},
    'ngap': {'UEContextReleaseComplete'},
    'e1ap': {'BearerContextReleaseComplete'},
}


def _detect_protocol(orig_name, probe_path=None):
    name = os.path.basename(orig_name).lower()
    for proto in _ID_FIELDS:
        if proto in name:
            return proto
    result = subprocess.run(
        ['tshark', '-r', probe_path or orig_name, '-c', '20', '-T', 'fields',
         '-e', 'frame.protocols'],
        capture_output=True, text=True,
    )
    for line in result.stdout.splitlines():
        for proto in _ID_FIELDS:
            if proto in line.lower():
                return proto
    raise SystemExit(
        f'error: cannot detect protocol in {orig_name!r}. '
        'File name or content must contain ngap, f1ap, or e1ap.'
    )


def _run_tshark(pcap_file, fields):
    args = ['tshark', '-r', pcap_file, '-T', 'fields']
    for f in fields:
        args += ['-e', f]
    try:
        result = subprocess.run(args, capture_output=True, text=True)
    except OSError as e:
        raise SystemExit(f'error: cannot run tshark: {e}')
    if result.returncode != 0:
        # Warnings, e.g. on unreadable preferences, come before the error.
        err = result.stderr.strip().splitlines()
        raise SystemExit(f'error: tshark failed on {pcap_file} (rc {result.returncode}): {err[-1] if err else ""}')
    return result.stdout.splitlines()


def _parse_val(raw, as_hex):
    """Return the first value from a (possibly comma-separated) tshark field."""
    first = raw.split(',')[0].strip()
    if not first:
        return None
    if as_hex:
        return hex(int(first))
    return first


def _parse_all_vals(raw, as_hex):
    """Return all values from a (possibly comma-separated) tshark field."""
    parts = [v.strip() for v in raw.split(',') if v.strip()]
    if as_hex:
        return [hex(int(v)) for v in parts]
    return parts


def _msg_name(info_col):
    """Extract the protocol message name from the tshark Info column."""
    # Info column format: "MessageName, optional details [optional bracketed suffix]"
    # Take the part before the first comma, then drop any " [...]" suffix.
    part = info_col.split(',')[0]
    bracket = part.find(' [')
    if bracket != -1:
        part = part[:bracket]
    return part.strip()


def _format_ts(epoch_str):
    return datetime.fromtimestamp(float(epoch_str), tz=timezone.utc).strftime('%H:%M:%S.%f')[:-3]


def _find_record(records, packet_ids):
    """Return the first active record that shares at least one ID with packet_ids."""
    for rec in records:
        for label, val in packet_ids.items():
            if rec.get(label) == val:
                return rec
    return None


# Pcaps listed in the error on a directory.
MAX_HINTS = 10


def _dir_hint(path):
    """Names of the pcaps of a directory, else the paths of those below it, for the error on a directory."""
    pcaps = sorted(n for n in os.listdir(path) if n.endswith('.pcap'))
    if pcaps:
        return f'; its pcaps: {", ".join(pcaps)}'
    below = sorted(os.path.relpath(os.path.join(d, n), path)
                   for d, _, names in os.walk(path) for n in names if n.endswith('.pcap'))
    if not below:
        return '; no pcap in or below it'
    # A test dir holds dozens of pcaps across its components.
    shown = ', '.join(below[:MAX_HINTS]) + (f', ... ({len(below)} in all)' if len(below) > MAX_HINTS else '')
    return f'; no pcap directly in it; pcaps below: {shown}'


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument('pcap_file', help='an F1AP, NGAP or E1AP pcap')
    args = ap.parse_args()
    if not os.path.isfile(args.pcap_file):
        hint = _dir_hint(args.pcap_file) if os.path.isdir(args.pcap_file) else ''
        print(f'error: {args.pcap_file} is not a pcap file{hint}', file=sys.stderr)
        sys.exit(1)

    # Stage for tshark (AppArmor restricts tshark to /tmp on Ubuntu); detect the
    # protocol from the original filename, falling back to probing the staged copy.
    pcap = _stage_for_tshark(args.pcap_file)
    proto = _detect_protocol(args.pcap_file, probe_path=pcap)
    fields_cfg = _ID_FIELDS[proto]
    labels = [f[0] for f in fields_cfg]
    new_ue_msgs = _NEW_UE_MSGS.get(proto, set())
    release_msgs = _RELEASE_MSGS.get(proto, set())

    tshark_fields = ['frame.number', 'frame.time_epoch', '_ws.col.Info'] + [f[1] for f in fields_cfg]
    lines = _run_tshark(pcap, tshark_fields)

    active = []  # list of dicts: {label: value}

    for line in lines:
        cols = line.split('\t')
        if len(cols) < 3 + len(fields_cfg):
            continue

        frame_num = cols[0]
        timestamp = _format_ts(cols[1])
        name = _msg_name(cols[2])
        id_cols = cols[3:]

        pkt_ids = {}
        # old_du_ue is ephemeral: shown on the line where it appears but not stored.
        # It comes from id-oldgNB-DU-UE-F1AP-ID (IE 47), which tshark returns as the
        # second comma-separated value of f1ap.GNB_DU_UE_F1AP_ID when present.
        old_du_ue = None
        for i, (label, _, as_hex) in enumerate(fields_cfg):
            raw = id_cols[i] if i < len(id_cols) else ''
            if label == 'du_ue':
                vals = _parse_all_vals(raw, as_hex)
                if vals:
                    pkt_ids['du_ue'] = vals[0]
                if len(vals) >= 2:
                    old_du_ue = vals[1]
            else:
                val = _parse_val(raw, as_hex)
                if val is not None:
                    pkt_ids[label] = val

        if not pkt_ids:
            continue

        if name in release_msgs:
            rec = _find_record(active, pkt_ids)
            if rec is not None:
                parts = [f'{l}={rec[l]}' for l in labels if l in rec]
                print(f'{frame_num}, {timestamp}, {name}, {", ".join(parts)} [released]')
                active.remove(rec)
            continue

        rec = None if name in new_ue_msgs else _find_record(active, pkt_ids)
        if rec is None:
            rec = {}
            active.append(rec)

        updated = any(rec.get(label) != val for label, val in pkt_ids.items())
        rec.update(pkt_ids)

        if updated:
            parts = []
            for l in labels:
                if l not in rec:
                    continue
                parts.append(f'{l}={rec[l]}')
                if l == 'du_ue' and old_du_ue is not None:
                    parts.append(f'old_du_ue={old_du_ue}')
            print(f'{frame_num}, {timestamp}, {name}, {", ".join(parts)}')


if __name__ == '__main__':
    main()

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Frames of a pcap: their one-line summaries, the decoding of one frame, and the time span of a pcap."""

from __future__ import annotations

import os
import struct
from typing import Any

from .tshark import Tshark

SUMMARY_FIELDS = ["frame.number", "frame.time_epoch", "frame.len", "_ws.col.Protocol", "_ws.col.Info"]
# Fields of the PDUs of MAC and RLC pcaps, by label. The ue_index of RLC is the DU UE index, see rlc_ue_index().
PDU_FIELDS: dict[str, list[tuple[str, str]]] = {
    "mac": [("rnti", "mac-nr.rnti"), ("direction", "mac-nr.direction")],
    "rlc": [
        ("ue_index", "rlc-nr.ueid"),
        ("bearer_type", "rlc-nr.bearer-type"),
        ("bearer_id", "rlc-nr.bearer-id"),
        ("direction", "rlc-nr.direction"),
    ],
}
_DIRECTIONS = {"0": "UL", "1": "DL"}
# Protocols decoded in full by decode(); the other layers of a frame get one line each.
DECODED_PROTOCOLS = "f1ap,ngap,e1ap,nr-rrc,nas-5gs,mac-nr,rlc-nr,pdcp-nr"

# Magic numbers of classic pcap files, by byte order and time resolution.
_PCAP_MAGICS = {
    b"\xd4\xc3\xb2\xa1": ("<", 1e-6),
    b"\xa1\xb2\xc3\xd4": (">", 1e-6),
    b"\x4d\x3c\xb2\xa1": ("<", 1e-9),
    b"\xa1\xb2\x3c\x4d": (">", 1e-9),
}
_PCAPNG_MAGIC = b"\x0a\x0d\x0d\x0a"


def is_pcap(path: str | os.PathLike[str]) -> bool:
    """Whether a file starts like a pcap or pcapng capture."""
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
    except OSError:
        return False
    return magic in _PCAP_MAGICS or magic == _PCAPNG_MAGIC


def time_span(path: str | os.PathLike[str]) -> tuple[float, float] | None:
    """Epochs of the first and last complete packets of a classic pcap, from its record headers, or None if it has
    none. A last packet cut short is left out, as tshark leaves it out.

    pcapng captures give None.
    """
    try:
        with open(path, "rb") as f:
            header = f.read(24)
            fmt = _PCAP_MAGICS.get(header[:4])
            if fmt is None or len(header) < 24:
                return None
            order, resolution = fmt
            size = os.fstat(f.fileno()).st_size
            first = last = None
            while len(rec := f.read(16)) == 16:
                sec, frac, incl_len, _ = struct.unpack(order + "IIII", rec)
                if f.seek(incl_len, os.SEEK_CUR) > size:
                    break
                t = sec + frac * resolution
                first = t if first is None else first
                last = t
    except OSError:
        return None
    return None if first is None else (first, last)


def summaries(tshark: Tshark, pcap: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """One-line summary of each frame: "frame", "epoch", "len", "protocol" and "info", as tshark shows them."""
    rows = (dict(zip(SUMMARY_FIELDS, values)) for values in tshark.iter_fields(pcap, SUMMARY_FIELDS, tag="frames-v1"))
    return [s for row in rows if (s := summary(row)) is not None]


def summary(row: dict[str, str]) -> dict[str, Any] | None:
    """Summary of a frame, see summaries(), given its tshark field values by name, or None without a number or time."""
    frame, epoch = row["frame.number"], row["frame.time_epoch"]
    if not frame or not epoch:
        return None
    return {
        "frame": int(frame),
        "epoch": float(epoch),
        "len": int(row["frame.len"] or 0),
        "protocol": row["_ws.col.Protocol"],
        "info": row["_ws.col.Info"],
    }


def pdus(tshark: Tshark, pcap: str | os.PathLike[str], proto: str) -> list[dict[str, Any]]:
    """The PDUs of a pcap of proto, "mac" or "rlc": "frame", "epoch", "len" and the fields of PDU_FIELDS by label.

    Directions are "UL" or "DL", and absent values None.
    """
    names = pdu_fields(proto)
    rows = (dict(zip(names, values)) for values in tshark.iter_fields(pcap, names, tag=f"{proto}-pdus-v1"))
    return [p for row in rows if (p := pdu(proto, row)) is not None]


def pdu_fields(proto: str) -> list[str]:
    """The tshark fields of the PDUs of proto, read by pdu()."""
    return ["frame.number", "frame.time_epoch", "frame.len", *(f for _, f in PDU_FIELDS[proto])]


def pdu(proto: str, row: dict[str, str]) -> dict[str, Any] | None:
    """The PDU of proto in a frame, see pdus(), given its tshark field values by name, or None without a number or
    time.
    """
    frame, epoch = row["frame.number"], row["frame.time_epoch"]
    if not frame or not epoch:
        return None
    out: dict[str, Any] = {"frame": int(frame), "epoch": float(epoch), "len": int(row["frame.len"] or 0)}
    for label, f in PDU_FIELDS[proto]:
        value = row[f]
        if label == "direction":
            value = _DIRECTIONS.get(value, value)
        elif f == "rlc-nr.ueid":
            value = rlc_ue_index(value)
        out[label] = value or None
    return out


def rlc_ue_index(ueid: str | None) -> str | None:
    """The DU UE index of an rlc-nr.ueid value, which OCUDU writes as the index + 1, or None without one."""
    try:
        index = int(ueid or "") - 1
    except ValueError:
        return None
    return str(index) if index >= 0 else None


def decode(tshark: Tshark, pcap: str | os.PathLike[str], frame: int) -> str:
    """Decoded tree of one frame, with the protocols of DECODED_PROTOCOLS in full."""
    lines = tshark.run(["-r", str(tshark.stage(pcap)), "-Y", f"frame.number == {int(frame)}", "-O", DECODED_PROTOCOLS])
    return "\n".join(lines)

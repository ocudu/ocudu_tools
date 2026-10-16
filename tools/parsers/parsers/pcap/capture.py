# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Everything about an OCUDU pcap from one tshark pass over its frames: its protocol, the summary of each frame, and its
messages or PDUs, as the other modules give them one pass each.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Any

from . import f1ap, frames, messages, run
from .tshark import Tshark

# Protocol of a pcap by the dissector of its first frame.
PROTOCOLS = {"ngap": "ngap", "f1ap": "f1ap", "e1ap": "e1ap", "mac-nr": "mac", "rlc-nr": "rlc"}


@dataclass
class Capture:
    """A pcap read in one pass. proto is "ngap", "f1ap", "e1ap", "mac" or "rlc", or None for other captures, which
    have only frames.

    frames are the summaries of frames.summaries(). messages are the messages of messages.messages() for NGAP, F1AP and
    E1AP, with the "rrc", "nas" and "crnti" of f1ap.carried() for F1AP, and their "links", see messages.links(). pdus
    are the PDUs of frames.pdus() for MAC and RLC.
    """

    proto: str | None
    frame_protocols: str
    frames: list[dict[str, Any]] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    pdus: list[dict[str, Any]] = field(default_factory=list)


def read(tshark: Tshark, pcap: str | os.PathLike[str]) -> Capture:
    """Reads a pcap: its protocol from its first frame, then all its frames in one pass with the fields of that
    protocol only, since each field adds to the time of a pass. A pcap without frames, e.g. of a process that wrote only
    its header, has the protocol of its file name, see run.pcap_proto().
    """
    first = tshark.run(["-r", str(tshark.stage(pcap)), "-c", "1", "-T", "fields", "-e", "frame.protocols"])
    if not first:
        return Capture(run.pcap_proto(pcap), "")
    frame_protocols = first[0]
    tokens = frame_protocols.split(":")
    proto = next((PROTOCOLS[d] for d in PROTOCOLS if d in tokens), None)
    rrc_types = f1ap.valid_rrc_types(tshark, pcap) if proto == "f1ap" else []
    if proto in ("mac", "rlc"):
        proto_fields = frames.pdu_fields(proto)
    elif proto is not None:
        proto_fields = messages.fields(proto, messages.valid_cause_fields(tshark, pcap, proto))
        if proto == "f1ap":
            proto_fields += f1ap.rrc_fields(rrc_types, f1ap.valid_nas_fields(tshark, pcap))
    else:
        proto_fields = []
    if proto in messages.LINK_FIELDS:
        proto_fields += tshark.valid_fields(pcap, messages.link_fields(proto))
    names = list(dict.fromkeys([*frames.SUMMARY_FIELDS, *proto_fields]))
    cap = Capture(proto, frame_protocols)
    # The fields are part of the tag, since they depend on the protocol, the tshark build and this code.
    tag = f"capture-{proto}-" + hashlib.sha256("\0".join(names).encode()).hexdigest()[:16]
    for values in tshark.iter_fields(pcap, names, tag=tag):
        row = dict(zip(names, values))
        if (summary := frames.summary(row)) is not None:
            cap.frames.append(summary)
        if proto in ("mac", "rlc"):
            if (pdu := frames.pdu(proto, row)) is not None:
                cap.pdus.append(pdu)
        elif proto is not None and (msg := messages.message(proto, row)) is not None:
            if proto == "f1ap":
                msg.update(f1ap.carried(row, rrc_types))
            msg["links"] = messages.links(proto, row)
            cap.messages.append(msg)
    return cap

# F1AP — pcap observation (CU ↔ DU, F1-C interface)

How F1AP shows up in `f1ap.pcap` and how to query it. The **semantics** —
procedure-code meanings, UE-arrival paths, identifier model, failure signatures —
are artifact-agnostic and live in `../../../common/protocols/f1ap.md`; this file is
the tshark/observation layer only.

The `f1ap.pcap` captures UE-context lifecycle messages, F1 infrastructure
messages (F1 Setup, gNB-CU/DU Configuration Update), and the RRC-container
transfers between CU and DU.

## Key tshark filters

```bash
# All F1AP messages with UE IDs
tshark -r f1ap.pcap \
    -T fields -E separator=$'\t' \
    -e frame.number -e frame.time_epoch \
    -e f1ap.procedureCode \
    -e f1ap.GNB_DU_UE_F1AP_ID -e f1ap.GNB_CU_UE_F1AP_ID

# UE context lifecycle
tshark -r f1ap.pcap -Y 'f1ap.procedureCode == 5 || f1ap.procedureCode == 6 || f1ap.procedureCode == 7'

# RRC container traffic (UL/DL RRC message transfer)
tshark -r f1ap.pcap -Y 'f1ap.procedureCode == 11 || f1ap.procedureCode == 12 || f1ap.procedureCode == 13'

# Specific procedures (codes/meanings: ../../../common/protocols/f1ap.md § Procedures and codes)
tshark -r f1ap.pcap -Y 'f1ap.procedureCode ==  1'   # F1Setup
tshark -r f1ap.pcap -Y 'f1ap.procedureCode ==  5'   # UEContextSetup
tshark -r f1ap.pcap -Y 'f1ap.procedureCode ==  6'   # UEContextRelease
tshark -r f1ap.pcap -Y 'f1ap.procedureCode ==  7'   # UEContextModification
tshark -r f1ap.pcap -Y 'f1ap.procedureCode == 11'   # InitialULRRCMessageTransfer
tshark -r f1ap.pcap -Y 'f1ap.procedureCode == 12'   # DLRRCMessageTransfer
tshark -r f1ap.pcap -Y 'f1ap.procedureCode == 13'   # ULRRCMessageTransfer
```

The procedure-code table and F1Removal (26) are in the common semantics doc.

## Identifier fields (tshark)

- `f1ap.GNB_DU_UE_F1AP_ID` — DU-assigned, present from InitialULRRCMessageTransfer.
- `f1ap.GNB_CU_UE_F1AP_ID` — CU-assigned, present once UEContextSetupRequest sent.
- `f1ap.C_RNTI` — present in InitialULRRCMessageTransfer.
- See `../cross-pcap-correlation.md` for joining to NGAP / E1AP, and
  `../../../common/identifiers.md` for the identifier model (scope, HO stability).

## UE-arrival signature (pcap view)

The two first-F1AP-message paths (proc 11 vs proc 5) and what they mean are in
`../../../common/protocols/f1ap.md` § UE arrival paths. In a handover test the
variation across UEs is expected, not an anomaly; for the full target-DU sequence
see [`../handover.md`](../handover.md).

## Detecting the empty-container reject (pcap technique)

The "can't serve" failure (`../../../common/protocols/f1ap.md` § Failure signatures)
is observed here by container **content length**, not IE presence:
`-T fields -e f1ap.DUtoCURRCContainer` → empty value ⇒ "can't serve" (a non-empty
value carries the CellGroupConfig for an admitted UE).

## RRC PDUs inside F1AP containers are NOT auto-dissected (tshark gotcha)

tshark dissects the RRC PDU carried in the RRC **message-transfer** procedures
(`InitialULRRCMessageTransfer`, `DL/ULRRCMessageTransfer`) and in
`UEContextSetup`/`Modification` — for those, `nr-rrc.<type>_element` fields fire.
But it does **not** dissect the `RRCContainer` inside a `UEContextReleaseCommand`
(proc 6): it prints that container as raw hex. So `tshark -V | grep -E
'rrcReject|rrcSetup|rrcRelease'` and filters like `nr-rrc.rrcReject_element` give
**false negatives** for the release-command case — the RRC message name never
appears even though it is present. Decode the `f1ap.RRCContainer` hex yourself
there; `SRBID` picks the channel (`SRBID=0` → CCCH, `SRBID≥1` → DCCH). A
security-activated DCCH release (`rrcRelease`) is PDCP-protected, so its bytes
are opaque; a `SRBID=0` CCCH container is a plain `rrcReject`/`rrcSetup`.

**In practice, use `f1ap_messages.py` (§ Parsing scripts)** — it resolves the RRC
message type per frame (including hand-decoding this SRB0/DL-CCCH release
container that tshark skips) and names the NAS message, so you don't `grep -V`
or decode bits by hand. The recipe and manual decode below are the underlying
mechanism — for a one-off check, or to understand what the script does.

```bash
# One-off: list the raw release-command containers (f1ap_messages.py decodes these for you)
tshark -r f1ap.pcap -Y 'f1ap.procedureCode==6 && f1ap.RRCContainer' \
    -T fields -e f1ap.RRCContainer | sort | uniq -c
```

Quick manual PER decode of a DL-CCCH message: bit0 = `message` choice (0=`c1`),
bits1–2 = `c1` choice (`00`=`rrcReject`, `01`=`rrcSetup`). Example: an admission
reject appears as `UEContextReleaseCommand`, `SRBID=0`, `RRCContainer: 09e0` →
`c1: rrcReject`, `waitTime=16s` → completely invisible to `grep rrcReject`.
Distinguish reject-in-release from a normal release by the container: a bare
release has no `RRCContainer`; a reject carries the short CCCH `rrcReject`.

The opposite gotcha: the `UEContextSetupRequest` of a handover target has no
`RRCContainer`, yet `nr-rrc.rrcReconfiguration_element` fires on it, from the
source's configuration inside `CUtoDURRCInformation` →
`HandoverPreparationInformation`, which is never sent to the UE. Match RRC
messages with `f1ap.RRCContainer` present; `f1ap_messages.py` reports `rrc` only
for a message with an RRC container.

## Parsing scripts

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/pcap/extract_proc_codes.py <f1ap.pcap> --proto f1ap
python3 ${CLAUDE_SKILL_DIR}/scripts/pcap/correlate_run.py <run-dir> --protocols f1ap

# Per-UE F1AP identity table (one row per UE)
python3 ${CLAUDE_SKILL_DIR}/scripts/pcap/f1ap_ue_ids.py <f1ap.pcap>

# Per-message F1AP timeline with decoded RRC + NAS message types (one row per
# message): frame, first_iso, message, rrc, nas, cu/du F1AP IDs, C-RNTI.
# Resolves the RRC type from tshark's dissection and hand-decodes the CCCH
# container (rrcReject/rrcSetup) that tshark leaves as hex in a release command.
# NAS type is named from tshark's decode (ciphered post-security NAS shows '-').
python3 ${CLAUDE_SKILL_DIR}/scripts/pcap/f1ap_messages.py <f1ap.pcap>
python3 ${CLAUDE_SKILL_DIR}/scripts/pcap/f1ap_messages.py <f1ap.pcap> --ue 640   # one UE by CU or DU F1AP id
python3 ${CLAUDE_SKILL_DIR}/scripts/pcap/f1ap_messages.py <f1ap.pcap> --rnti 4601 # whole UE context of a C-RNTI: 4601, 0x4601 or 17921
python3 ${CLAUDE_SKILL_DIR}/scripts/pcap/f1ap_messages.py <f1ap.pcap> --nas      # only NAS-bearing msgs
```

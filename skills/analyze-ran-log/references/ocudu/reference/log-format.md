# OCUDU gNB Log Format Reference

## File: gnb.log

### Header (the CONFIG echo)

```
2026-05-18T18:18:27.405677 [GNB     ] [I] Built in RelWithDebInfo mode using commit <sha> on branch <branch>
2026-05-18T18:18:27.407955 [CONFIG  ] [D] gNB input configuration (all values):
gnb_id: 411
gnb_id_bit_length: 32
ran_node_name: ocudugnb01
... <full effective config dump, no timestamp on body lines> ...
2026-05-18T18:18:28.074323 [CONFIG  ] [I] Worker pool "main_pool" instantiated with #workers=5 ...
```

The CONFIG echo is the effective config (after all merges and defaults). It is
**not** the same as `ocudu_gnb.yml` on disk — the YAML file contains only the
user-supplied keys, the CONFIG echo contains every key including defaults.
Skip the echo for general analysis; only consult it when the user asks about
the effective value of a specific knob.

The first **real event** is the `[CONFIG  ] [I] Worker pool` line. From there
on, every event begins with an ISO-8601 timestamp.

### End of file

A clean shutdown ends with:

```
... [GNB     ] [I] Closing PCAP files...
... [ALL     ] [I] Saved PCAP (DLT=252) to "<path>" and closed
... [ALL     ] [I] Workers stopped successfully.
```

An abnormal exit (crash, kill -9) leaves no `Workers stopped successfully.`
line. A SIGTERM-style shutdown completes the sequence above.

### Log line format

```
YYYY-MM-DDTHH:MM:SS.uuuuuu [LAYER   ] [LVL] MESSAGE
```

- **Timestamp**: ISO-8601 with microsecond precision, no timezone suffix
  (local time of the host).
- **Layer tag**: 8 chars, space-padded, in square brackets. Common tags listed
  below.
- **Level**: `D` (debug), `I` (info), `W` (warning), `E` (error). There is no
  critical level. In the typical info-level run only `I` (and a few `D` from
  CONFIG) appear. Some messages change form with the level of their layer,
  e.g. PRACH detections in `[SCHED]` (see Procedure markers).
- **Message**: free-form, often with structured fields like `ue=0`,
  `c-rnti=0x4601`, `pci=1`, `[    33.1]` (slot indicator: SFN.subframe).

### Layer tags

| Tag | Subsystem | Notes |
|---|---|---|
| `[GNB     ]` | Top-level binary | Start/stop banner, build info, PCAP open/close (`[CU      ]` / `[DU      ]` in the split `cu` / `du` binaries) |
| `[APP     ]` | Application | Resource-usage / energy reader, app-level notices |
| `[CONFIG  ]` | Config parser | Echoed config + worker-pool init lines |
| `[ALL     ]` | Cross-cutting | Worker lifecycle, PCAP save/close |
| `[IO-EPOLL]` | I/O reactor | fd registrations, UDP rx counts |
| `[SCTP-GW ]` | NGAP transport | N2 bind/connect lifecycle |
| `[UDP-GW  ]` | GTP-U transport | Bind, MMSG settings |
| `[NGAP    ]` | NGAP protocol | NG setup + UE-associated PDUs (`Tx PDU`, `Rx PDU`) |
| `[XNAP    ]` / `[CU-CP-XN]` | XnAP protocol / Xn-C gateway | Inter-gNB Xn setup and handover PDUs |
| `[NRPPA   ]` | NRPPa protocol | Positioning |
| `[E1AP    ]` | E1AP ASN.1 conversion | Rare; E1AP PDUs are under `CU-CP-E1` / `CU-UP-E1` |
| `[E2AP    ]`, `[E2SM-KPM]`, `[E2SM-RC ]`, `[E2SM-CCC]`, `[E2-DU   ]`, `[E2-CU-CP]`, `[E2-CU-UP]` | E2 agent | Only with E2 enabled |
| `[CU-CP   ]` | CU control-plane | UE creation, routines (Initial Context Setup, UE Removal), AMF connection |
| `[CU-CP-F1]` | CU side of F1AP | UEContextSetup, DL/UL RRC message transfer |
| `[CU-CP-E1]` | CU-CP side of E1AP | E1 setup, BearerContext{Setup,Modification,Release} |
| `[CU-UP   ]` | CU-UP user-plane | UE (bearer context) creation, F1-U attach, `Modified DRB`, PDU session disconnect |
| `[CU-UP-E1]` | CU-UP side of E1AP | E1 setup, BearerContext echoes |
| `[CU-F1-U ]` / `[F1-U    ]` | CU side of F1-U | UL data path |
| `[CU-UEMNG]` | CU UE manager | (rare; usage varies) |
| `[DU      ]` | DU top-level | DU-High start/stop |
| `[DU-MNG  ]` | DU manager | UE Create/Delete, cell limits, SIB1 dump |
| `[DU-F1   ]` | DU side of F1AP | UEContextSetup ack, UEContextRelease |
| `[DU-F1-U ]` | DU side of F1-U | DL data path |
| `[RLC     ]` | RLC layer | Bearer config (`RLC AM configured`), TX/RX PDU per DRB/SRB |
| `[RRC     ]` | RRC layer | CCCH/DCCH UL/DL message names (`rrcSetup`, `rrcReconfiguration`, etc.) |
| `[PDCP    ]` | PDCP layer | TX/RX PDU per DRB/SRB |
| `[SDAP    ]` | SDAP | QoS flow mapping |
| `[ROHC    ]` | ROHC | Header compression (only when configured) |
| `[GTPU    ]` | GTP-U | UL TX, DL RX, tunnel adds |
| `[MAC     ]` | MAC scheduler/PDU | DL PDU, UL subPDU, procedure progress |
| `[SCHED   ]` | Slot-level scheduler | `Slot decisions pci=N` lines, PRACH events, cell creation |
| `[PHY     ]` | Lower-PHY events | PDCCH, PDSCH, PUCCH, PUSCH, PRACH detection |
| `[OFH     ]` | Open fronthaul (split 7.2) | eCPRI/C-plane/U-plane timing, late/early packets |
| `[RU      ]` / `[RF      ]` | RU / radio session | SDR RU and radio driver lifecycle |
| `[HWACC   ]`, `[EAL     ]` | Hardware acceleration / DPDK | Only with DPDK or accelerators |
| `[NTN     ]` | NTN | Orbital / Doppler updates (NTN configs only) |
| `[SEC     ]` | Security | K_gNB and derived keys (logged as empty when hex disabled) |
| `[METRICS ]` | Metrics emitter | Per-period scheduler metrics row in log |
| `[FAPI    ]` | FAPI interface | (rare in current runs) |
| `[zmq:rx:N:N]` / `[zmq:tx:N:N]` | ZMQ radio driver | TX/RX bind, "Waiting for data" idle messages after stop |
| `[SCTP    ]`, `[ASN1    ]`, `[F1AP-ASN1-PCK]`, `[E1AP-ASN1-PCK]`, `[E2-SUBSCRIBER]` | Support | Rare: DTLS, ASN.1 and packer errors, E2 subscriptions |

The exact set of tags present depends on which layers are enabled — see
`config-format.md` § Common overrides and what they enable.

### Procedure markers

These free-text fragments — easy to grep, layer-agnostic — mark important
state transitions:

| Pattern | Meaning |
|---|---|
| `Built in .* mode using commit` | Binary build identity (top of file) |
| `Closing PCAP files...` | Shutdown handler entered (often follows SIGTERM) |
| `Workers stopped successfully` | Clean process termination |
| `[SCTP-GW ] N2: SCTP connection to AMF established` | NGAP transport (SCTP) up |
| `[CU-CP ] N2: Connection to AMF on <ip>:<port> was established` | AMF connection up (the marker the summary keys on) |
| `Tx PDU: NGSetupRequest` / `Rx PDU: NGSetupResponse` | gNB ↔ AMF NG setup |
| `"E1AP CU-UP Setup Procedure" finalized` | Internal E1 between CU-CP and CU-UP up |
| `DU created successfully` / `O-DU created successfully` | DU-High initialised |
| `SIB1 cell=N: { ... }` | Cell broadcast config dumped (one big JSON block per cell) |
| `==== gNB started ===` *(in stdout.log only)* | Service ready to accept UEs |
| `Cell creation idx=N` *(in `[SCHED]`)* | First slot tick for that cell |
| `Processed slot events pci=N: prach(ra-rnti=0xN preamble=N ssb=N tc-rnti=0xN)` *(in `[SCHED]`, info level; `ssb=` optional, `msgb-rnti=` for 2-step)* | RA preamble detected → MSG2 scheduled (4-step) |
| `Processed slot events pci=N:` then continuation line `- PRACH: slot=S preamble=N ra-rnti=0xN temp_crnti=0xN ta_cmd=N` *(in `[SCHED]`, debug level)* | Same event at debug level: the header line ends with `:` and each event is a `- ` line below it, without preamble, so grep the continuation line, not the header |
| `MsgB: msgb-rnti=0xN ... tbs=T` *(in `[SCHED]`)* | 2-step RA MsgB scheduled. `tbs=12` = successRAR-only (`S=0`, spec-legal — see `../../common/procedures/random-access.md` § 2-step RA type); a following separate PDSCH under the new `c-rnti` then carries the RRC response |
| `Rx PDU du=N tid=N du_ue=N: InitialULRRCMessageTransfer` *(in `[CU-CP-F1]`)* | UE's first RRC msg crossed F1 |
| `UE created` *(in `[CU-CP]`)* | UE context entered CU-CP |
| `CCCH UL rrcSetupRequest` / `CCCH DL rrcSetup` / `DCCH UL rrcSetupComplete` | Initial RRC connection establishment |
| `Tx PDU ue=N ran_ue=N: InitialUEMessage` *(in `[NGAP]`)* | NAS forwarded to AMF |
| `Rx PDU ... amf_ue=N: InitialContextSetupRequest` *(in `[NGAP]`)* | AMF setting up UE security + bearers |
| `"Initial Context Setup Routine" initialized/finished` *(in `[CU-CP]`)* | UE attach progress |
| `DCCH DL securityModeCommand` / `DCCH UL securityModeComplete` | AS security activation |
| `DCCH DL ueCapabilityEnquiry` / `DCCH UL ueCapabilityInformation` | UE capability exchange |
| `DCCH DL rrcReconfiguration` / `DCCH UL rrcReconfigurationComplete` | Bearer/cell reconfig (may include HO when body contains `reconfigurationWithSync`) |
| `BearerContextSetupRequest/Response` *(in `[CU-CP-E1]`/`[CU-UP-E1]`)* | DRB bearer creation across E1 |
| `BearerContextModificationRequest/Response` | DRB modification (mid-call) |
| `BearerContextReleaseCommand/Complete` | DRB teardown |
| `Disconnecting PDU session with psi=N` *(in `[CU-UP]`)* | PDU session torn down |
| `Rx PDU ... UEContextReleaseCommand` *(in `[NGAP]`)* | AMF asked to release the UE |
| `DCCH DL rrcRelease` | RRC connection release sent to UE |
| `"UE Removal Routine" finished successfully` *(in `[CU-CP]`)* | UE context fully torn down |
| `"UE Delete": Procedure finished successfully` *(in `[DU-MNG]`)* | DU side of UE removal done |
| `RRC container not ACKed within a time window of` *(in `[DU-F1]`)* | UE didn't ACK the DL release in time — common but benign in simulated runs |
| `reconfigurationWithSync` *(in RRC body, multi-line)* | Handover command body |
| `HandoverRequired` / `HandoverCommand` / `HandoverRequest` / `HandoverRequestAcknowledge` *(NGAP)* | Inter-gNB handover signaling |
| `reestablishmentRequest` / `rrcReestablishment` / `rrcReestablishmentComplete` *(RRC)* | RRC reestablishment (post-RLF) |
| `RLF detected. Cause: N consecutive undecoded CSIs` *(in `[MAC]`)* / `RLF detected with cause "..."` *(in `[DU-MNG]`)* | Explicit RLF marker (this build logs it — don't assume it's absent). Two distinct triggers exist: UL HARQ-KO (`max_consecutive_kos`, PUSCH) and consecutive undecoded PUCCH format-2 CSI reports — name which one fired |

### Common structured fields

| Field | Format | Where |
|---|---|---|
| `ue=N` | Decimal UE index local to CU-CP | RRC / CU-CP / CU-CP-F1 / NGAP / GTPU / PDCP |
| `du_ue=N` | UE index local to DU | F1AP-related lines |
| `cu_ue=N` | UE index local to CU on F1 | F1AP-related lines |
| `ran_ue=N` | RAN UE NGAP ID | NGAP |
| `amf_ue=N` | AMF UE NGAP ID | NGAP (only after first DL from AMF) |
| `cu_cp_ue=N` / `cu_up_ue=N` | UE index on each side of E1 | E1AP |
| `c-rnti=0xNNNN` | DU-assigned C-RNTI (hex) | MAC / RRC / SCHED |
| `tc-rnti=0xNNNN` | Temporary C-RNTI from MSG2 | SCHED PRACH lines |
| `ra-rnti=0xNN` | RA-RNTI (preamble-derived) | SCHED PRACH lines |
| `pci=N` | Physical cell ID | SCHED / MAC / DU-MNG |
| `du_cell_index=N` | DU's internal cell index | DU-MNG |
| `[ SFN.SUB]` | Slot indicator (12 chars, right-aligned) | SCHED / MAC / PHY |
| `tid=N` | E1AP transaction ID | E1AP setup |
| `teid=0xNNNNNNNN` | GTP-U TEID | GTPU / CU-UP |
| `qfi=QFI=N` | QoS Flow Identifier | GTPU |
| `psi=N` | PDU Session ID | CU-UP |
| `crc=OK` / `crc=KO` | UL PUSCH decode result | PHY |
| `sinr=N.NdB` | SNR estimate | PHY |
| `tbs=N` | Transport block size in bytes | SCHED / MAC |

### Metrics row (per-period summary)

When `metrics.enable_log: true` and `layers.enable_sched: true`, the `[METRICS]`
tag emits one line every ~1s like:

```
[METRICS ] Scheduler cell pci=1 metrics: total_dl_brate=14.6Mbps total_ul_brate=6.56Mbps nof_prbs=270 nof_dl_slots=897 nof_ul_slots=897 total_prach_preambles=1 error_indications=0 ... failed_dl_pdcch=0 failed_common_dl_pdcch=0 failed_ul_pdcch=0 failed_common_ul_pdcch=0 failed_uci=0 nof_ues=1 ... msg3_ok=1 msg3_nok=0 late_dl_harqs=0 late_ul_harqs=0 ... events=[{rnti=0x4601 slot=34.3 type=ue_create}, {rnti=0x4601 slot=41.1 type=ue_reconf}]
```

Useful fields:
- `total_dl_brate` / `total_ul_brate` — throughput on that cell that period.
- `total_prach_preambles` (up to v26.04: `nof_prach_preambles`) — PRACH attempts
  seen in the period. Match both with `(nof|total)_prach_preambles`.
- `msg3_ok` / `msg3_nok` — MSG3 decode outcome counters.
- `late_dl_harqs` / `late_ul_harqs` / `failed_dl_pdcch` / `failed_ul_pdcch` /
  `failed_uci` — scheduler-detected errors (should be 0 in healthy runs).
  `failed_common_dl_pdcch` / `failed_common_ul_pdcch` are the common-SS part of
  the PDCCH counts. Older builds log one `failed_pdcch`; match all with
  `failed_(common_)?(dl_|ul_)?pdcch=[1-9]`.
- `nof_ues` — UEs active in the cell that period.
- `events=[...]` — discrete scheduler events with slot timestamps:
  `ue_create`, `ue_reconf`, `ue_remove`, `ue_reestablish`, `harq_ack_timeout`,
  `prach`, ...

---

## File: stdout.log

Short file (30–300 lines for clean runs; thousands for long multi-UE traffic
runs where the metrics table is reprinted every period). Layout:

```
--== OCUDU gNB (commit <sha>) ==--

Lower PHY in executor sequential baseband mode.
Available radio types: uhd and zmq.
Cell pci=1, bw=50 MHz, 1T1R, dl_arfcn=368500 (n3), dl_freq=1842.5 MHz, dl_ssb_arfcn=364090, ul_freq=1747.5 MHz

N2: Connection to AMF on 172.20.0.10:38412 completed
Remote control server listening on 0.0.0.0:8001
==== gNB started ===
Type <h> to view help

          |--------------------DL---------------------|-------------------------------UL-----------------------------
 pci rnti | cqi  ri  mcs  brate   ok  nok  (%)  dl_bs | pusch  rsrp  ri  mcs  brate   ok  nok  (%)    bsr     ta  phr
   1 4601 |  14 1.0   25    15M  438    0   0%  2.88k |  60.4  -3.0   1   27  6.56M   55    0   0%      0      0   38
   ...
Stopping...
Logfile stored in /var/log/retina/<run>/gnb.log
RLC PCAP stored in /var/log/retina/<run>/rlc.pcap
```

**Banner block** — first ~10 lines. Contains:
- The built commit hash (matches the `[GNB] Built in ... commit X` line in `gnb.log`).
- Per-cell `pci=N, bw=N MHz, NTNR, dl_arfcn=..., dl_freq=...` line (one per cell).
- `N2: Connection to AMF on <ip>:<port> completed` confirms NGAP setup.
- `==== gNB started ===` is the green-light marker.

**Metrics table** — one row per period per active C-RNTI per cell:

| Col | Meaning |
|---|---|
| `pci` | Cell PCI |
| `rnti` | UE C-RNTI in hex |
| `cqi` / `ri` / `mcs` / `brate` / `ok` / `nok` / `(%)` / `dl_bs` | DL: CQI, rank, MCS, throughput, successful TBs, NACKs, BLER%, DL buffer |
| `pusch` / `rsrp` / `ri` / `mcs` / `brate` / `ok` / `nok` / `(%)` | UL: PUSCH SNR (dB), RSRP (dBm), rank, MCS, throughput, OKs, NACKs, BLER% |
| `bsr` | UL buffer status |
| `ta` | Timing advance (sample units, with optional `n`/`p` suffix for nanos / picos) |
| `phr` | Power headroom |

**Shutdown block** — `Stopping...` followed by paths of the produced logfile
and PCAPs. Absence of these lines means an abnormal exit.

In multi-cell runs the header reprints whenever a UE moves cells, and the
`pci` column shows the UE's serving cell.

`n/a` values in the DL columns appear when the UE hasn't reported CQI yet
(very early in attach).

---

## File: metrics.json

A standard JSON **array** of per-period records (one object per element, the
whole file wrapped in `[ ... ]`). Records alternate between MAC latency
snapshots and per-cell scheduler snapshots:

```
[{"du": {"du_high": {"mac": {"dl": [{"average_latency_us": 81.5, ..., "pci": 1}]}}}, "timestamp": "..."},
 {"cells": [{"cell_metrics": {...}, "event_list": [...], "ue_list": [...]}], "timestamp": "..."},
 ...]
```

Parse the whole file with `json.load(open("metrics.json"))` — it is valid JSON,
so do **not** try to split it line-by-line (the first and last lines carry the
`[` and `]` and won't parse on their own). Never `cat` it into context; these
files reach hundreds of KB.

Key paths:
- `cells[].cell_metrics.*` — per-period totals (latencies, error counters).
- `cells[].event_list[]` — discrete scheduler events (`ue_create`,
  `ue_reconf`, `ue_remove`, ...) with `slot` (`SFN.subframe`) and `rnti`.
- `cells[].ue_list[]` — per-UE PHY metrics (CQI, MCS, BLER, BSR, throughput).

---

## Key grep recipes

```bash
# Skip the CONFIG echo — find the first real event after it
grep -n "^[0-9].* \[CONFIG  \] \[I\] Worker pool" gnb.log | head -1

# All RRC messages (CCCH + DCCH, with line numbers)
grep -nE "\[RRC     \] \[I\]" gnb.log

# NGAP procedures (one line per Tx/Rx PDU)
grep -nE "\[NGAP    \] \[I\]" gnb.log

# F1AP procedures
grep -nE "\[CU-CP-F1\]|\[DU-F1   \]" gnb.log | grep -E "Tx PDU|Rx PDU"

# E1AP procedures
grep -nE "\[CU-CP-E1\]|\[CU-UP-E1\]" gnb.log | grep -E "Tx PDU|Rx PDU"

# All UE creations
grep -n "UE created" gnb.log

# All UE removals (CU-CP side)
grep -n '"UE Removal Routine" finished successfully' gnb.log

# Initial context setup outcomes
grep -nE '"Initial Context Setup Routine" (initialized|finished)' gnb.log

# Handover-related (RRC body has reconfigurationWithSync; NGAP for inter-gNB)
grep -nE "reconfigurationWithSync|HandoverRequired|HandoverCommand|HandoverRequest|HandoverNotify" gnb.log

# RRC reestablishment
grep -nE "reestablishmentRequest|rrcReestablishment(Complete)?" gnb.log

# PRACH attempts (scheduler-level; info form, then debug continuation lines)
grep -nE "\[SCHED   \].*prach\(|^- PRACH: " gnb.log

# PHY CRC failures
grep -n "crc=KO" gnb.log

# Scheduler metrics rows (one per period per cell)
grep -nE "\[METRICS \] Scheduler cell" gnb.log

# Warnings / errors (no `[W]` in healthy runs; check anyway)
grep -nE "\[(W|E)\] " gnb.log | head -50

# Run boundaries
grep -nE "Built in .* commit|Workers stopped successfully|Closing PCAP files" gnb.log

# AMF NGAP connection lifecycle
grep -nE "(N2:|NGSetupRequest|NGSetupResponse|NGSetupFailure|AMF reconnection)" gnb.log

# All bearer (DRB) lifecycle events
grep -nE "BearerContext(Setup|Modification|Release)(Request|Response|Command|Complete)" gnb.log

# Per-UE scope: filter any of the above by ue=N or c-rnti=0xNNNN
grep -nE "ue=0 " gnb.log | grep -E "\[(RRC|NGAP|CU-CP)" | head -50

# Shutdown signal (process got SIGTERM)
grep -nE "Stopping CU-CP|CU-CP stopped|Closing PCAP files" gnb.log
```

For multi-UE runs, anchor on `c-rnti=0x46XX` (DU-side) or `ue=N` (CU-side).
Note that the same UE has different IDs on each side: `ue` on CU-CP differs
from `du_ue` on F1 and from `ran_ue`/`amf_ue` on NGAP.

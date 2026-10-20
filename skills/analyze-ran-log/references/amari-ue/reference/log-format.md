# Amarisoft UE Log Format Reference

## File: ue.log

### Header (first lines, start with `#`)

```
# lteue version 2025-09-19, gcc 13.2.0, glibc 2.17, Linux x86_64 ...
# Licensed to 'Software Radio Systems (SRS)' [...]
# Log file format:
# time layer dir ue_id {cell_id rnti sfn channel:} message
# SMP DRBs=1 RF0=8/3
# CFG=<base64-encoded config>
# Started on 2026-05-18 18:17:35
```

The `# Ended on` line appears at the very end of the file when the UE exited
cleanly (graceful quit). An abnormal exit (crash or kill) leaves no end marker.

### Log line format

```
HH:MM:SS.mmm [LAYER] DIR FIELDS... MESSAGE
```

Fields vary by layer (see per-layer table below). The timestamp has millisecond
precision. Lines with no leading timestamp are **continuation lines** (multi-line
PDU message bodies, indented with spaces).

### Per-layer format

| Layer | Format | Notes |
|---|---|---|
| NAS | `HH:MM:SS.mmm [NAS] -  UE_ID New state : GMM_STATE CM_STATE` | State transition |
| NAS | `HH:MM:SS.mmm [NAS] DL/UL UE_ID CHANNEL: MSG_TYPE` | NAS message |
| RRC | `HH:MM:SS.mmm [RRC] DL f000 CELL_ID CHANNEL: MSG_TYPE` | Broadcast (before attach); the UE_ID field is the fixed pseudo-id `f000`, not an SFN |
| RRC | `HH:MM:SS.mmm [RRC] DL/UL UE_ID CELL_ID CHANNEL: MSG_TYPE` | Dedicated (after attach) |
| PHY | `HH:MM:SS.mmm [PHY] DL UE_ID CELL_ID RNTI SFN.SF CHANNEL: PARAMS` | DL channel event |
| PHY | `HH:MM:SS.mmm [PHY] UL UE_ID CELL_ID RNTI SFN.SF CHANNEL: PARAMS` | UL channel event |
| MAC | `HH:MM:SS.mmm [MAC] DL/UL UE_ID CELL_ID LCID:len ... ` | MAC PDU |
| RLC | `HH:MM:SS.mmm [RLC] DL/UL UE_ID CHANNEL SN=N` | RLC PDU |
| PDCP | `HH:MM:SS.mmm [PDCP] UL UE_ID CHANNEL SN=N` | PDCP SDU |
| PROD | `HH:MM:SS.mmm [PROD] -  - SIM-Event: EVENT_TYPE` | Simulation event |
| TRX | `HH:MM:SS.mmm [TRX] -  MESSAGE` | RF driver info |

**UE_ID**: 4-char hex (e.g. `0001`, `000a`, `0080`). In multi-UE runs it ranges
up to `ue_count`, so values above `0009` are hex.
**CELL_ID**: 2-char decimal (e.g. `00`, `01`).
**RNTI**: 4-char hex (e.g. `4601`, `ffff` for broadcast).
**SFN.SF**: `NN.M` — system frame number dot subframe (e.g. `16.1`).

### NAS state values

| GMM State | CM State | Meaning |
|---|---|---|
| 5GMM-NULL | CM-IDLE | Initial / after full deregistration |
| 5GMM-REGISTERED-INITIATED | CM-IDLE | Registration request sent, no RRC connection yet |
| 5GMM-REGISTERED-INITIATED | CM-CONNECTED | Registration request sent, RRC connected |
| 5GMM-REGISTERED | CM-CONNECTED | Fully attached with PDU session |
| 5GMM-REGISTERED | CM-IDLE | Attached but RRC idle |
| 5GMM-DEREGISTERED-INITIATED | CM-CONNECTED | Deregistration in progress |
| 5GMM-DEREGISTERED | CM-CONNECTED | Deregistered (RRC still up momentarily) |
| 5GMM-NULL | CM-CONNECTED | Transitional before idle release |

### PROD sim event types

| Event | Meaning |
|---|---|
| `power_on` | UE turns on, starts cell search |
| `power_off` | UE initiates deregistration / detach |
| `quit` | UE process exits |
| `cbr_recv` | Start constant-bit-rate DL traffic |
| `cbr_send` | Start constant-bit-rate UL traffic |
| `ping` | Send ICMP pings |
| `pdn_connect` | UE requests an additional PDU session |
| `pdn_disconnect` | UE releases a PDU session |
| `rrc_reest` | Trigger an RRC reestablishment (simulated radio link failure) |

### Key PHY channel keywords

| Keyword in PHY line | Meaning |
|---|---|
| `PSS:` | Primary sync signal detected (cell found) |
| `PBCH:` | Physical broadcast channel decoded (SSB) |
| `PDCCH:` | DL control channel (scheduling grant) |
| `PDSCH:` | DL shared channel (data) — check `crc=OK` vs `crc=FAIL` |
| `PUSCH:` | UL shared channel (data) |
| `PUCCH:` | UL control channel (ACK/NACK, SR, CSI) |
| `PRACH:` | Random access preamble transmitted |
| `CSIRS:` | CSI-RS measurement |
| `CSIIM:` | CSI-IM measurement |
| `CSI:` | CSI report (rank, CQI, PMI) |

### RRC channel keywords

| Channel | Meaning |
|---|---|
| `BCCH-BCH-NR` | MIB on PBCH |
| `BCCH-NR` | SIB1, SIB2, … on PDSCH |
| `CCCH-NR` | RRC Setup Request / RRC Setup / RRC Reject / RRC Reestablishment Request |
| `DCCH-NR` | Dedicated connection messages (RRC Reconfiguration, etc.) |

### Key RRC message types (in DCCH-NR)

| Message | Direction | Meaning |
|---|---|---|
| RRC reconfiguration | DL | Bearer/cell config update; body may contain `reconfigurationWithSync` (→ handover) |
| RRC reconfiguration complete | UL | Sent on **target cell** after HO |
| RRC reestablishment request | UL | UE requests reestablishment after RLF |
| RRC reestablishment | DL | Network responds to reestablishment |
| RRC reestablishment complete | UL | Reestablishment done |
| RRC setup | DL | Initial connection setup |
| RRC setup complete | UL | Connection established |
| Security mode command | DL | Activate AS security (note: **no** `RRC ` prefix in the log) |
| Security mode complete | UL | AS security activated (note: **no** `RRC ` prefix in the log) |
| RRC release | DL | Network releases the RRC connection |

---

## File: stdout.log

Short file (typically 30–100 lines). Contains:

```
Warning, CPU hyperthreading is enabled, ...
Warning: /var/log/.../amarisoft_ue.cfg:40: unused property 'tx_time_offset'
UE version 2025-09-19, Copyright (C) 2012-2025 Amarisoft
This software is licensed to Software Radio Systems (SRS).
License server: [masked] ([masked])
Support and software update available until 2026-10-28.

RF0: sample_rate=122.880 MHz dl_freq=1842.500 MHz ul_freq=1747.500 MHz (band n3) dl_ant=1 ul_ant=1
2026-05-18T18:17:34.972732 [ALL     ] [I] Task worker "async_thread" started...
...
(ue)
Press [return] to stop the trace
Cell 0: SIB found
Cell 1: SIB found      ← (only in multi-cell tests)
---...---DL---------- ---------------------UL-
UE_ID  RAT CL RNTI   CFO   SRO  SINR   RSRP  mcs retx rxfail txok brate     #its  mcs  ta retx   tx brate
    1   NR 00    -   ...
    1   NR 00 4601   ...
[192.168.3.2:2000] CBR_RECV: sent 8929, recv 8929
[192.168.3.2:2001] CBR_SEND: sent 8555, recv 8555
2026-05-18T18:18:02.016607 [ALL     ] [I] Task worker "async_thread" finished.
```

**UE stats table columns**: UE_ID, RAT, CL (cell index), RNTI (hex), CFO (Hz),
SRO (ppm), SINR (dB), RSRP (dBm), DL mcs, DL retx, DL rxfail, DL txok, DL brate,
#its (DL HARQ iterations), UL mcs, UL ta, UL retx, UL tx count, UL brate.

CBR stats: `sent` = packets sent by the source, `recv` = packets acknowledged/received
at the sink. `recv < sent` indicates packet loss.

In multi-cell runs the CL column shows the UE's current serving cell; the RNTI
changes on every handover. The startup `Warning, ... hyperthreading` and
`Warning: ... unused property ...` lines are expected for OCUDU-driver configs and
do not indicate errors.

---

## File: amarisoft_ue.cfg

JSON5 config (cell parameters, UE identity/credentials, and the `sim_events`
scenario script). Full structure and field reference: `config-format.md`.

---

## Key grep recipes

```bash
# NAS state transitions (all)
grep -n "New state" ue.log

# NAS states for a specific UE
grep -n "New state" ue.log | grep " 0001 "

# All RRC messages (header lines only)
grep -n "\[RRC\]" ue.log | grep -v "^#"

# Key DCCH messages
grep -n "\[RRC\].*DCCH-NR:" ue.log

# Handovers (reconfigurationWithSync appears in body)
grep -n "reconfigurationWithSync {" ue.log

# RRC reestablishment messages
grep -n "\[RRC\] .* RRC reestablishment" ue.log

# PRACH attempts
grep -n "PRACH:" ue.log

# PHY CRC failures
grep -n "crc=FAIL" ue.log

# All PROD/sim events
grep -n "SIM-Event" ue.log

# Failure records. ue.log has no severity field; failures read e.g.
# "Contention resolution failed or timer expired", "Random access failure".
grep -nE "^\S+ \[(MAC|RRC|NAS|RLC|PDCP|PROD)\] .*\b([Ff]ail(ed|ure)?|[Ee]rror|expired|[Rr]eject)\b" ue.log

# CBR traffic results
grep -E "CBR_RECV|CBR_SEND" stdout.log

# UE RF configuration
grep "^RF" stdout.log

# Cell discovery
grep "SIB found" stdout.log

# Log duration
grep -E "^# (Started|Ended)" ue.log
```

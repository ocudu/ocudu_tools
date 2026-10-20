# VIAVI Command Log Format Reference

The artifact the **VIAVI RU simulator** (TM500-family network tester) writes while
driving an OCUDU test. It emulates the RU, RF channel, a population of NR UEs, and
the core, and logs every control command it sends, the tester's responses, and a
runtime event stream.

## File: `YYMMDD_HHMMSS_Command_Log<NNN>.txt`

Plain text, **CRLF** line endings, can reach hundreds of thousands of lines /
tens of MB. A matching `.zip` holds the **identical** `.txt` (a compressed copy,
not a distinct artifact). Some bytes are non-UTF-8, so always use
`LC_ALL=C grep -a` when grepping by hand.

### Line grammar

```
DD/MM/YY HH:MM:SS:mmm <payload>
```

- **Date is day-first** (`18/06/26` = 18 Jun 2026); the millisecond field is
  separated by a **colon**, not a dot (`10:58:14:039`).
- During the shutdown phase a literal `... ` is inserted between the timestamp and
  payload (`14:58:47:947 ... C: ELOG 0x00 Ok`).
- **Continuation lines** carry no timestamp — they are the body of the record
  above (an RRC event's `Cell Info:`, a `REGISTRATION IND` body).
- **GETSTATS rows are timestamped**: every row of a dump repeats the timestamp of
  its `UE ID:` header, so a line grep returns only the header; take the rows with
  `grep -a -A30` or `viavi_log_search.py`, which groups them with their header.

### Payload classes

| Class | Shape | Meaning |
|---|---|---|
| Command echo | `RSET`, `SELR 0 …`, `CFGR 0 SCS 1 1`, `CONF ORU …`, `STRT`, `SETP …`, `SDLI …`, `FORW …`, `WAIT FOR …`, `MEAS …` | A control command the test script sent to the tester |
| Command response | `C: <CMD> 0xNN Ok …` | Result of the preceding command; `0x00` = success (every response in a healthy run is `0x00`) |
| Info event | `I: CMPI <subsystem> <event…>` | The runtime event stream — RA, RRC, NAS/MTE, O-RAN, RLC (see § CMPI events) |
| Tester engine | `I: TMAE 0xN Information/Warning - …`, `TMA_Warning: …` | Tester measurement-engine info/warnings |

### Setup / framing commands (start of file)

| Command | Meaning |
|---|---|
| `RSET` | Reset the tester |
| `SCXT <idx> <LTE\|NR>` | Define a system context (typically `0:LTE`, `1:NR`) |
| `SELR <idx> …`, `CFGR <idx> SCS <n> <n>` | Select & configure a radio / sub-carrier spacing index |
| `CONF ORU <RU> <key> <val>` | O-RAN RU emulator settings (MAC, MTU, RachVer, …) |
| `SCFG MTS_MODE` | Enable multi-test-scenario mode |
| `WAIT FOR "<str>" … TIMEOUT <s>` | Block until a runtime string appears (e.g. `ORAN: CU PLANE ACTIVE`) |
| `STRT` | Start the test |
| `SETP <PARAM> <val>` / `SDLI <ITEM> …` | Set a runtime parameter / enable a data-logging item |
| `FORW … GETSTATS …` | Request a per-UE statistics dump (see § GETSTATS) |

The tail of a clean run shows `STOP_LOGGING`, then `DISCONNECT` (`C: DISCONNECT 0x00 OK`).

### CMPI events (`I: CMPI …`) — the analysis-relevant signal

| Event line (after `I: CMPI`) | Meaning |
|---|---|
| `L2 Random Access Initiated :UE Id:N (<trigger>: Cell Id C, Dl Freq F, SSB Id S)` | PRACH started. `<trigger>` ∈ `Connection Establish`, `Handover`, `SR MAX Exceeded`, `SR NO Resource`. `Dl Freq` is in 100-kHz units (`37050` = 3705.0 MHz) |
| `L2 Random Access Complete :UE Id:N (TC-RNTI: 0xXXXX, TimingAdv: T, PreambleTxCount: P)` | PRACH succeeded; `PreambleTxCount` = attempts needed |
| `L2 Random Access Error :UE Id:N (Result: <reason>, TC-RNTI: -, …, PreambleTxCount: P)` | PRACH failed; e.g. `Result: Max_Preambles_Exceeded` |
| `L2 Random Access Cancelled :UE Id:N (…)` | PRACH aborted |
| `RRC Cell Selection: UE Id: N` (+ `Cell Info:` body) | UE (re)selected a cell |
| `RRC Handover Complete: UE Id: N` (+ `Old/New Cell Info:` body) | HO finished onto the new cell |
| `RRC RRC Connection Re-establishment Started: UE Id: N` | Reestablishment began (often right after an RA error) |
| `RRC UE Id:N: RRC Re-establishment Dropped (…): Check whether RRC_DISABLE_CONNECTION_REESTABLISHMENT setp enabled or UE not registered` | Reestablishment not attempted |
| `RRC UE Id:N: User Triggered RRC Re-establishment Error (…): AS security not activated …` | Reestablishment refused before AS security / DRB setup |
| `MTE 0 NR LOWER LAYER FAILURE IND:UE Id:N` | Radio link lost below RRC (RLF) |
| `MTE 0 NR CONNECTION IND:UE Id:N` | RRC connection up |
| `MTE 0 NR DISCONNECTION IND:UE Id:N` | RRC connection released |
| `MTE 0 NR CONNECTION FAILED IND:UE Id:N` | Connection attempt failed; the multi-line body gives the cause, e.g. `RRC: RRC Connection Establishment timed out (T300/T319 expired)` (UE never got a valid RRCSetup) |
| `MTE 0 NR REGISTRATION IND:UE Id:N` (+ PLMN / PDU session / DNN body) | 5GMM registration accepted |
| `MTE 0 NR DEREGISTRATION IND:UE Id:N` | Deregistered |
| `MTE 0 NR PDU SESSION MODIFICATION IND:UE Id:N` | PDU session modified |
| `MTE 0 NR PLMN LOSS IND` / `PLMN RECOVERY IND` | Lost / recovered PLMN |
| `ORAN_RU <RU>: ORAN: CU PLANE ACTIVE` or `ORAN_RU ORAN: CU PLANE ACTIVE (Ru <n> on server)` | O-RAN C-plane up for that RU (bring-up gate) |
| `LN RLC maximum retransmissions reached` | RLC hit max retx (radio-link trouble) |
| `NRLNULCTRL Warning …` / `NRLNDLSRP Warning …` | UL power-control / DL SRP warnings |

**UE Id** is a plain decimal (`0`–`N`), written `UE Id:113` (no space) on most
event lines and `UE Id: 113` (with a space) on RRC events — match both with
`UE I[dD]:\s*`.

### GETSTATS dump

Triggered by `C: FORW 0x00 Ok MTE GETSTATS [<stats>] [<ue-list>] [<ue-list>]`. Each
dump covers the UE subset in `<ue-list>`; a measurement round fires several dumps
back-to-back, so iterate across all of them rather than trusting one. A
`[COMBINED]` UE list gives one record for all UEs, headed by a UE ID range
(`UE ID: 0-32   (Radio Context: …`); its throughput is the sum over the UEs, not
a per-UE value. Per UE (every row carries the header's timestamp, stripped here):

```
UE ID: 68   (Radio Context: 0   Cell ID: 1   DL Freq: 3705.060 MHz)
BCH NO DATA
DBCH NO DATA / PCH NO DATA / RACH NO DATA          ← idle channels
DL-SCH (PCC)
  Detects: … Non-Detects: … DRX: …
  Throughput: <inst>  Average: <avg>  Min: …  Max: <peak>     ← bits/s
  Codeword: 0
  [NumTBs] [NumTBErrors] [BLER] [Total Bits] [Total Bit Errors] [BER]
  00000000000563 00000000000000  0.0000000000 00000003881560  -  -
UL-SCH (PCC)
  Throughput / Average / Min / Max
  [NumTBs] [NumTBsNack] [NumTBsAck] [NumBits] [NumBitsAck] [BLER] …
DL HARQ (PCC) / UL HARQ (PCC)                       ← per-HARQ-process tables
  HARQ: 0 … HARQ: 15  with [NumTBs] [NumTBErrors] [BLER] …
```

Throughput fields are **bits/s** as reported by the tester (`Average` is the
running mean; `Max` the peak). BLER is the standalone `0.xxxxxxxxxx` float in the
SCH data row. The numeric columns are zero-padded fixed-width.

## File: `*.zip`

Same basename as the `.txt`, containing the identical `.txt`. The helper scripts
read it in place via `zipfile`; prefer the `.txt` when both are present.

## Key grep recipes

All use `LC_ALL=C grep -a` (binary-safe) and should be capped with `| head -n 200`.

```bash
L="<command-log>.txt"

# Random access: initiated / complete / error / cancelled counts
for k in Initiated Complete Error Cancelled; do \
  echo -n "$k "; LC_ALL=C grep -ac "Random Access $k" "$L"; done

# RA failure reasons
LC_ALL=C grep -aoE 'Random Access Error.*Result: [A-Za-z_]+' "$L" \
  | grep -oE 'Result: [A-Za-z_]+' | sort | uniq -c

# Connection / registration lifecycle counts
for k in "NR CONNECTION IND" "NR DISCONNECTION IND" "NR CONNECTION FAILED IND" \
         "NR REGISTRATION IND" "NR DEREGISTRATION IND"; do \
  printf '%6d  %s\n' "$(LC_ALL=C grep -ac "$k" "$L")" "$k"; done

# Everything about one UE (decimal id), with its multi-line bodies
LC_ALL=C grep -aA3 'UE Id:113\b\|UE Id: 113\b' "$L" | head -n 200

# UE Id <-> gNB C-RNTI bridge (cross-artifact join — see correlate/ue-identity-map.md)
LC_ALL=C grep -a 'Random Access Complete' "$L" \
  | grep -oE 'UE Id:[0-9]+ \(TC-RNTI: 0x[0-9A-Fa-f]+'

# Mobility
LC_ALL=C grep -ac 'RRC Handover Complete' "$L"
LC_ALL=C grep -ac 'RRC Connection Re-establishment Started' "$L"
LC_ALL=C grep -ac 'RRC Re-establishment Dropped\|RRC Re-establishment Error' "$L"
LC_ALL=C grep -ac 'NR LOWER LAYER FAILURE IND' "$L"

# O-RAN bring-up gate
LC_ALL=C grep -a 'CU PLANE ACTIVE' "$L"

# Failed command responses (anything other than 0x00)
LC_ALL=C grep -aoE 'C: [A-Z_]+ 0x[0-9A-Fa-f]+' "$L" | grep -v ' 0x00$'

# GETSTATS dumps (count the C: responses, not the command echoes), per-UE and
# COMBINED records, and one UE's record with its timestamped rows
LC_ALL=C grep -ac 'C: FORW .* GETSTATS' "$L"
LC_ALL=C grep -acE '^[0-9/]* [0-9:]* UE ID: [0-9]+ ' "$L"
LC_ALL=C grep -acE '^[0-9/]* [0-9:]* UE ID: [0-9]+-[0-9]+ ' "$L"
LC_ALL=C grep -a -A30 'UE ID: 113 ' "$L" | head -n 200

# Run time span (first / last timestamp)
LC_ALL=C grep -aoE '^[0-9]{2}/[0-9]{2}/[0-9]{2} [0-9:]{12}' "$L" | sed -n '1p;$p'
```

Prefer the helper scripts (`viavi_log_summary.py`, `viavi_log_search.py`) over
hand grep — they handle the `.zip`, CRLF, multi-line blocks and GETSTATS records
for you.

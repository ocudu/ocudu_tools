# Procedure: throughput & BLER (VIAVI GETSTATS)

Throughput and block-error stats come from **GETSTATS** dumps, requested by
`C: FORW 0x00 Ok MTE GETSTATS [ALL] [<ue-list>] [COMBINED]`. Each dump covers the
UE subset in `<ue-list>`, and a measurement round fires several dumps in a row,
so always aggregate across dumps rather than reading one. See
`../reference/log-format.md` § GETSTATS dump for the full block layout.

## What to read

Per UE, the dump opens with:

```
UE ID: N   (Radio Context: 0   Cell ID: C   DL Freq: F MHz)
```

then DL and UL shared-channel blocks:

```
DL-SCH (PCC)
  Throughput: <inst>  Average: <avg>  Min: …  Max: <peak>      ← bits/s
  [NumTBs] [NumTBErrors] [BLER] [Total Bits] …
  <numTBs>  <numErr>  0.0000000000  <bits>  -  -               ← BLER = the 0.xxxx float
UL-SCH (PCC)
  Throughput / Average / Min / Max
  [NumTBs] [NumTBsNack] [NumTBsAck] [NumBits] [NumBitsAck] [BLER] …
```

- **Throughput fields are bits/s.** `Average` is the running mean; `Max` is the
  observed peak; `Throughput` is the latest instantaneous value (often `0` between
  bursts).
- **BLER** is the standalone `0.xxxxxxxxxx` float in the SCH data row (DL-SCH and
  UL-SCH each have one). The per-HARQ-process tables under `DL HARQ`/`UL HARQ`
  carry their own per-process BLER — don't confuse them with the SCH BLER.
- `BCH/DBCH/PCH/RACH NO DATA` lines are normal for an idle UE.

## How to get the numbers

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/viavi/viavi_log_summary.py <log>
```

The **Throughput / BLER (GETSTATS)** section reports the dump count, the per-UE
and COMBINED (`UE ID: 0-32`, all UEs summed) record counts, and for each kind the
peak DL/UL average throughput and max DL/UL SCH BLER across its records —
representative without picking one unrepresentative dump (early/teardown dumps
cover only 1–2 UEs). Do not compare a COMBINED throughput with a per-UE target.

For one UE's records over time (each with its stat rows, which carry the header's
timestamp):

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/viavi/viavi_log_search.py <log> \
  --pattern "UE ID: N " --after <HH:MM:SS:mmm> --before <HH:MM:SS:mmm>
```

By hand, grep the header's timestamp, which every row repeats (`-A30` cuts a
record of over 90 rows short): `LC_ALL=C grep -a "^$(LC_ALL=C grep -a -m1 'UE ID: N ' <log> | cut -c1-21) " <log>`.

## Reading the result

| Observation | Interpretation |
|---|---|
| Peak avg throughput far below the configured GBR/MBR | UE not being scheduled to target rate — check the gNB scheduler / grant pattern |
| High DL-SCH BLER (» a few %) with retx in the HARQ tables | DL radio quality / MCS too aggressive |
| UL-SCH BLER high | UL coverage / power control — correlate with `NRLNULCTRL Warning` lines |
| `Throughput` 0 but `Average` non-zero | bursty traffic — the dump landed between bursts |

Throughput/BLER are the tester's measurement of what the **OCUDU gNB** delivered;
to attribute a shortfall, cross-check the gNB metrics/log and the MAC pcap.

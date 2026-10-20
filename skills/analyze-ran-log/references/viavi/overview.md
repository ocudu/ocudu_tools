# VIAVI — overview slot

Type-specific slot for the **overview** activity; the driving playbook pulls
this in.

## run summary script

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/viavi/viavi_log_summary.py <command-log>
```

One streaming pass (handles `.txt` and `.zip`) emitting:

- **Test setup** — time span + duration, system contexts (`SCXT`), MTS mode, RUs
  that reached `CU PLANE ACTIVE`, SCS index, Cell IDs, DL frequencies.
- **UE population** — distinct `UE Id` count and range.
- **Random access** — Initiated / Complete / Error / Cancelled counts, completion
  rate, and the breakdown of error `Result` reasons.
- **Connection / registration** — CONNECTION / DISCONNECTION / CONNECTION FAILED /
  REGISTRATION / DEREGISTRATION / PDU-session-modification IND tallies.
- **Mobility** — Handover Complete and Re-establishment Started / Dropped / Error
  counts.
- **Throughput / BLER** — GETSTATS dump count, per-UE and COMBINED record counts,
  and per kind the peak DL/UL average throughput (bits/s) and max DL/UL SCH BLER
  across all dumps. COMBINED values sum all UEs.
- **Errors / warnings** — failed command responses, TMAE warnings, RLC max-retx.
- **Anomalies** — RA errors, CONNECTION FAILED, LOWER LAYER FAILURE,
  Re-establishment Dropped / Error, RLC max-retx, non-`0x00` responses, each with
  its UE Ids.

If the script is unavailable, fall back to the grep recipes in
`reference/log-format.md`.

## summary block

```
## VIAVI Command-Log Overview

**Path:** <command-log>

### Setup
- Duration: Xh Ym  (HH:MM:SS → HH:MM:SS)
- Contexts: 0:LTE, 1:NR   RUs: RU1, RU2 (CU PLANE ACTIVE)
- SCS index N, Cell IDs …, DL freq … MHz

### UEs & lifecycle
- Distinct UE Ids: N (range a–b)
- RA: I initiated / C complete (P%) / E error / X cancelled
- Connection: conn / disc / failed ; Registration: reg / dereg
- Mobility: H handovers, R reestablishments started (D dropped, E errors)

### Throughput / BLER
- GETSTATS dumps: D ; per-UE and COMBINED: peak DL/UL avg tput, max DL/UL BLER

### Anomalies
- <bullet per anomaly, or "None">
```

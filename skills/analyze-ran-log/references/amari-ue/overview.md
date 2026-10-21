# Amarisoft UE — overview slot

Type-specific slot for the **overview** activity; the driving playbook pulls
this in.

## inventory

Resolve already reported the run dir and which of `ue.log`, `stdout.log`,
`amarisoft_ue.cfg` are present.

## run summary script

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/amari-ue/ue_log_summary.py <run-dir | UE log file>
```

A file is parsed as given (e.g. `ue0.log`), with its directory's `stdout.log`,
`amarisoft_ue.cfg` and `metrics.json`; a directory needs a `ue.log` at or under it.

Emits, in one pass:

- UE configuration (band, BW, UE count, IMSI, sim events)
- Run start/end time and duration
- UE software version and RF port info
- NAS state timeline (deduped, state changes only)
- Key RRC messages (setup, reconfigurations, reestablishments)
- Procedure counts: PRACH attempts, handovers, reestablishments, PHY CRC failures,
  error log lines (non-PHY records with fail/failed/failure/error/expired/reject,
  e.g. `Contention resolution failed or timer expired`; ue.log has no severity field)
- NOTE when `log_options` sets `all.level` to warn/error: the log counts undercount
- `metrics.json`: cumulative message counters of the last `stats` record (PRACH, RRC
  setup, registration, PDU session), per-cell DL/UL bitrate (last interval) and DL
  error / UL retx counts summed over all records
- Traffic stats: CBR sent/received with loss percentage
- Sim events timeline (power_on, cbr_recv/cbr_send, power_off, quit)
- Anomalies (packet loss > 1%, PHY errors, unexpected final NAS state; from
  `metrics.json`: PRACH > 2× RRC setups + reestablishments (RA failures),
  registration accepts below the UE count, `*_reject` counters; a UL test, cbr_send
  `bit_rate` ≥ 1 kbit/s, with `CBR_SEND: sent 0` or no CBR_SEND result)

If the script is absent or fails, fall back to the grep recipes in
`reference/log-format.md`.

## stdout quick-scan

`stdout.log` is short — read it in full to capture:

- UE version
- RF port configuration (frequencies, bands)
- Cell(s) SIB found
- Final CBR stats

## summary block

```
## Amarisoft UE Run Overview

**Path:** <run-dir>
**Test:**  <parent test dir name, if visible>

### Configuration
- UE count: N [single-UE | multi-UE]
- Band: nXX, BW: YY MHz
- Sim events: power_on → cbr_recv/send → power_off → quit
- UE version: <version>

### Timeline
- <time>  Started
- <time>  [UE 0001] 5GMM-REGISTERED CM-CONNECTED
- <time>  Handover (cell 00 → 01)     ← if any
- <time>  [UE 0001] 5GMM-NULL CM-IDLE
- <time>  Ended (duration: Xs)

### Procedures
(from `metrics.json` counters when the log level is warn)
- PRACH: N attempts
- Handovers: N
- Reestablishments: N
- PHY CRC failures: N

### Traffic
- DL: sent=N, recv=M (X.X% loss)
- UL: sent=N, recv=M (X.X% loss)

### Anomalies
- <bullet per anomaly, or "None">
```

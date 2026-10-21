# OCUDU — overview slot

Type-specific slot for the **overview** activity; the driving playbook pulls this
in. Assumes the input is resolved to a run directory and that the § Efficiency
rules apply.

## inventory

Resolve already reported the run dir and which of `gnb.log`, `stdout.log`,
`ocudu_gnb.yml`, `metrics.json` are present. Also note any sibling `*.pcap` (defer
those to the `pcap` type) and multi-component setups — a sibling `ocudu-cu-cp-*/`
or `ocudu-du-*/` means the analysis here covers *this* component only; don't merge
them in a per-component overview.

## run summary script

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/ocudu/ocudu_log_summary.py <run-dir>
```

Emits, in one pass:

- Build: commit and branch of the log, and the stdout banner commit when it differs.
- Configuration, from the config file or else the log echo: node type and planes, gnb_id, cell (pci, band, BW, SCS,
  ARFCN, antennas), duplex (from the TDD pattern, else the band; unknown without either), AMF endpoint, log levels,
  enabled pcaps.
- Run lifecycle: first and last log time, duration, AMF SCTP connection, NG setup outcome, clean shutdown.
- Cells and UEs: cells created, UE creations, Initial Context Setups completed, releases.
- Procedure counts: RRC reconfigurations, handovers completed (target side, info: `"Intra CU Handover Target Routine"
  finished`, conditional intra-CU `CHO inter-DU completion finalized` and NGAP `HandoverNotify`; Xn and inter-CU CHO
  not counted), `reconfigurationWithSync` in RRC bodies (`rrc_level: debug` only), NGAP HandoverRequired/HandoverRequest
  PDUs, reestablishments, bearer setups/modifications/releases, PRACHs, PHY CRC failures, with a note when they are
  unreliable: RRC, NGAP, E1AP or CU at `warning` in the config, or, without config levels, no info or debug lines of
  RRC, NGAP, CU-CP or CU-CP-E1 in the log. No note for a log of no CU-CP, by its banner (DU, CU-UP, DU low), its
  configured planes or its loggers, nor for RRC when CU-CP logs at info and no UE was created. Initial Context Setups
  are compared with the UEs of an RRC setup, and not without RRC or CU-CP lines.
- Scheduler metrics: from `metrics.json` when present (peak DL/UL bitrate per UE, max latency, late HARQs, failed
  PDCCH/UCI allocations, error indications, Msg3 outcomes, events), else the count and last of the METRICS
  rows, without the leveled lines of the metrics workers.
- Anomalies: abnormal exit (no closing `(END)` line), NG setup failure or no response, reestablishments, incomplete
  Initial Context Setups, high CRC failures, Msg3 failures, failure events (RLF, RRC setup timeout, ConRes timeout,
  ...: count and first of each type), error and warning line totals with the count and first line of the 5 loggers
  with most, late HARQs, failed PDCCH allocations, error indications, fewer releases than creations at a clean
  shutdown.

If the script is absent or fails, fall back to the grep recipes in
`reference/log-format.md` § Key grep recipes to collect the same info manually.

For UE-lifecycle latency questions (per-stage and end-to-end
creation/configuration/removal timings), load `latency-profiling.md` — it
documents `scripts/ocudu/ocudu_ue_proc_latency.py`. Not needed for ordinary
overviews.

## stdout quick-scan

`stdout.log` is short for single-UE runs — read it in full to capture:

- gNB version banner (`OCUDU gNB (commit ...)`).
- Per-cell freq config lines.
- `N2: Connection to AMF on ...` success/failure.
- `==== gNB started ===` (start marker) presence.
- Final `Stopping...` / `Logfile stored in ...` / `RLC PCAP stored in ...` lines
  (absence ⇒ abnormal exit).

For multi-UE runs `stdout.log` reprints the metrics table many times — use
`head -n 50` + `tail -n 30` instead of `cat`.

## summary block

```
## OCUDU gNB Run Overview

**Path:** <run-dir>
**Test:** <parent test dir name, if visible>
**Build:** commit <sha> on branch <branch>

### Configuration
- gNB ID: <id> · RAN node: <name>
- Cells: pci=<N> band=n<X> BW=<Y> MHz <T>T<R>R dl_arfcn=<A> dl_freq=<F> MHz
- AMF: <ip>:<port> (NG setup: success | failure cause=<X>)
- Log levels enabled: rrc=<L>, ngap=<L>, f1ap=<L>, pdcp=<L>, mac=<L>, phy=<L>
- PCAPs: rlc, ngap, f1ap, e1ap, mac (only the ones enabled)
- Duplex: TDD <pattern> | FDD

### Timeline
- <time>  gNB started (AMF connected)
- <time>  ue=0 c-rnti=0x4601 created  (Initial Context Setup → DRB up)
- <time>  Handover ue=0 cell pci=1 → pci=2     ← if any
- <time>  ue=0 released (cause from NGAP UEContextReleaseCommand)
- <time>  gNB stopped (clean)

### Procedures
- UE attaches:    N    Initial Context Setup OK: K/N
- RRC reconfig:   N
- Handovers:      N    (NGAP handover msgs: N)
- Reestablishments: N
- Bearer setups:  N    modifications: N    releases: N
- PRACH events:   N    PHY CRC failures: N
- Warnings:       N    Errors: N

### Traffic
- Peak DL/UL per UE: <X>/<Y> Mbps · max latency <L> us
- Late DL/UL HARQs: N/N · failed PDCCH/UCI allocs: N/N
- Msg3 ok/nok: N/N

### Anomalies
- <bullet per anomaly, or "None">
```

The traffic figures are **per-UE peaks, not per-cell**, and the script computes
**no BLER** — don't invent either. `Initial Context Setup OK: K/N` is the single
most useful multi-UE health signal; carry it even when K == N.
N counts the RRC setups in the log; completions of UEs set up before the log
start are reported apart and are not in K.

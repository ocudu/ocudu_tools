# Procedure: Throughput degradation

Use this when the run completes but DL or UL throughput is lower than
expected for the cell BW / MCS, or when the metrics rows show high BLER or
late HARQs.

## Quick read

The `metrics.json` is the fastest source. The summary script already prints
peak DL/UL bitrate, max latency, late HARQs, failed PDCCH allocations, and
MAC error indications.

In `gnb.log` the `[METRICS]` rows are the per-period summary. The
`stdout.log` reproduces the same data in a human-readable table.

## Where to look

| Signal | Source | Notes |
|---|---|---|
| Peak DL/UL throughput | `metrics.json` → `cells[].ue_list[].dl_brate` / `.ul_brate` | Compare against `bw=N MHz` x MCS for the band |
| MCS | `metrics.json` → `cells[].ue_list[].dl_mcs` / `.ul_mcs` | Capped low ⇒ link adaptation bringing it down |
| CQI / RI | `metrics.json` → `ue_list[].cqi` / `.dl_ri` | Low CQI ⇒ poor radio conditions |
| BLER | `metrics.json` → `ue_list[].dl_nof_nok / (dl_nof_ok + dl_nof_nok)` | Or columns `nok` and `(%)` in `stdout.log` |
| Late HARQs | `metrics.json` → `cell_metrics.late_dl_harqs` / `.late_ul_harqs` | Signal that the scheduler couldn't honour the HARQ timing |
| Failed PDCCH allocations | `metrics.json` → `cell_metrics.failed_dl_pdcch` + `failed_ul_pdcch` (`failed_common_*_pdcch` are part of them; builds before June 2026: `nof_failed_pdcch_allocs`) | Scheduler overload / coreset crowded |
| MAC error indications | `metrics.json` → `cell_metrics.error_indication_count` | Should be 0 in healthy runs |
| Buffer status | `dl_bs` / `bsr` columns in `stdout.log` | Persistent high values without throughput ⇒ scheduler/PDCCH issue |
| Power headroom | `phr` in `stdout.log` | <10 = UE near its TX power limit |
| Timing advance | `ta` in `stdout.log` (sample units, suffix `n` ns, `p` ps) | Drifting TA ⇒ clock issue / UE moving |

## Investigation checklist

1. Read the summary script's "Scheduler Metrics" section first.
2. If any of `late_dl_harqs`, `late_ul_harqs`, the failed PDCCH counters
   (`failed_dl_pdcch` / `failed_ul_pdcch` / `failed_common_{dl,ul}_pdcch`; older
   builds: one `failed_pdcch`), `error_indications` are non-zero, find the
   timestamp window where they spiked:
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/ocudu/ocudu_log_search.py gnb.log --layer METRICS \
       --pattern "late_dl_harqs=[1-9]|failed_(common_)?(dl_|ul_)?pdcch=[1-9]" --max-lines 20
   ```
3. Compare MCS to channel quality. With high SNR (`pusch_snr_db > 25`) the
   MCS should be near the cell's max (27 for 256QAM, 28 for 64QAM); much
   lower MCS at high SNR ⇒ link adaptation issue.
4. If only DL throughput is low, check whether the test workload was
   DL-limited (CBR rate). Compare `dl_brate` to the configured CBR rate
   from the UE side (the UE log captures `cbr_recv` / `cbr_send`).
5. Latency histogram: `cells[].cell_metrics.latency_histogram` is a 10-bin
   distribution. The right-most bins growing over time ⇒ scheduler back-
   pressure.

## Common root causes

- **PDCP left at info in a performance test** — chatty PDCP logging at
  info-level can itself slow the gNB. Confirm `pdcp_level: warning` in
  `ocudu_gnb.yml` for performance tests.
- **`low_phy_dl_throttling` set in config** — `ru_sdr.expert_cfg.low_phy_dl_throttling`
  non-zero throttles DL on purpose.
- **ZMQ simulator host overloaded** — the gNB's metric latency spikes correlate
  with `Late HARQs`. Check `top` / `ps_info_gnb.txt`.
- **Wrong PRACH config** — many PRACH preambles (`total_prach_preambles`; up to
  v26.04: `nof_prach_preambles`) but no UE creations.
- **Spectrum overlap / wrong ARFCN** — UE log won't find the cell (handoff to
  the `amari-ue` type).

## Cross-references

- `phy-issues.md` — radio link signals.
- UE log: handoff to the `amari-ue` type to see UE-side throughput
  (CBR_RECV / CBR_SEND).

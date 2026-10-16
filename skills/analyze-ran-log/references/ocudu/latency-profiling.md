# UE-lifecycle latency profiling

Load this only when the task is *how long UE creation / reconfiguration /
removal takes* (per-stage and end-to-end). For everyday overviews/failures it is
not needed.

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/ocudu/ocudu_ue_proc_latency.py <gnb.log> [proc-filter-substr]
```

The filter matches in any case (`sched` keeps `Sched UE Config`); with no match it lists the procedures of the log.

**Prerequisite — L2 log levels.** The DU/MAC `proc="..."` traces this tool keys
on are emitted only when `du_level` (DU-manager) and `mac_level` (MAC — which also
gates SCHED; see `reference/config-format.md` § Common overrides) are at `info`/`debug`. In a Release build or
any run with these at `warning` the traces are absent and the profiler prints
`no DU UE procedure in the log` — this is a config artifact, not a fast run. Check the summary's
`Log levels` / `L2 traces` line first (run `ocudu_log_summary.py`); if it reports
L2 traces suppressed, the per-stage latencies are simply unavailable from that
log. Note this also means a monolithic gNB log can look CU-only at a glance when
only `rrc`/`cu` are at `info`.
At `info`, `MAC UE Creation`, `MAC UE Reconfiguration`, `MAC UE Removal` and
`Sched UE Config` log only their `done` stage, so the profiler prints `no latency`
for them: their starts need `mac_level=debug`. `UE Create`, `UE Configuration` and
`UE Delete` have their latencies at `info`.

Reconstructs every UE-lifecycle procedure instance and reports per-stage and
total latency (n, mean, p50, p90, p99, max), grouped into CREATION
(`UE Create` + `MAC UE Creation`), CONFIGURATION (`UE Configuration` +
`MAC UE Reconfiguration` + `Sched UE Config`) and REMOVAL (`UE Delete` +
`MAC UE Removal` + `UE Context Release`). The optional second arg filters by a
procedure-name substring (e.g. `"UE Delete"`).

Key properties:

- **Declarative + graceful degradation.** Stages live in a registry of
  `proc="..."` markers. Builds that emit only `Procedure started/finished` yield
  the end-to-end total; builds with the granular MAC/DU stage traces get the
  full per-stage breakdown. Absent stages are skipped (`n=0`), so the same tool
  works on mainline and instrumented branches.
- **Instance-correct.** Records are keyed per `(ue, procedure)` and re-opened on
  each procedure start, so UE-id reuse across re-creations and multiple
  reconfigurations per UE are counted as separate instances.
- **Parallel-stage aware.** Procedures may define explicit transitions
  (`TRANSITIONS`) for stages that race — e.g. MAC UL/DL context creation is
  measured from MAC start, and the commit from `max(UL, DL)`.

Reading the output: a flat, low-variance stage (small mean≈p99 gap) points at a
fixed timer; a large mean-vs-p50 gap points at burst/queue contention. Compare
branches/builds on mean + tail to see which phase dominates.

Extending it: add an entry to the `PROCS` registry (procedure name → ordered
`(stage_key, exact_label)` list) and, for racing stages, a `TRANSITIONS` entry.

## Attributing UE-lifecycle latency

Reusable checklist for *where* a stage's time may come from. Don't assume
executor queueing — check these signals and let the log decide. Verify file:line
against current source before quoting.

### UE removal — candidate contributors

- **Scheduler removal deferral.** The scheduler can intentionally hold a UE
  before freeing it: `ue_repository::schedule_ue_rem` sets
  `rem_slot = last_sl_tx + get_max_slot_ul_alloc_delay(ntn_cs_koffset) + 1`, and
  `slot_indication` frees the UE only once `sl_tx >= rem_slot` **and**
  `is_ue_ready_for_removal` (all DL/UL HARQs empty) — to avoid PUCCH collisions
  with CSI/SR PDUs already in the resource grid and to let HARQs drain. To
  measure it, the scheduler logs the actual free as SCHED
  `ue=N rnti=…: UE has been successfully removed.` (gated by `mac_level`); its gap
  from the removal-procedure start is the deferral, separate from any
  control-executor resume after it.
- **RRC-Release guard.** The `ran_resource_release_timeout` → MAC
  `min_removal_delay` guard is applied only when the F1AP `UE Context Release`
  carries an RRC container; without one the F1AP `Started → Initiate UE release`
  gap is instead `srb_flush_grace_period`. Check which path applies before
  attributing that gap.
- **Cross-executor hops.** The DL/UL MAC `remove_ue` steps hop between control
  and cell/UL executors and can queue under bursts.
- **Often negligible** (confirm with a direct measurement rather than assuming):
  `deallocate_ue_buffers`, `ue_mng.remove_ue`, the control-executor resume after
  scheduler completion.

### Creation — candidate contributors

Per-stage MAC context creation involves cross-executor handshakes that are
slot-quantized; the post-context scheduler-commit steps tend to carry the tail
under concurrency. A small median with a large, UE-count-dependent tail points to
queue contention rather than a single slow operation.

### Reading the shapes

- Flat, low-variance stage (mean ≈ p50 ≈ p99) ⇒ a fixed timer/deferral.
- Large mean-vs-p50 gap that grows with UE count ⇒ burst / executor-queue
  contention.
- To split a stage that bundles a hop + a synchronous op, add a direct
  `steady_clock` measurement around the op (log the elapsed µs) rather than
  inferring from the stage delta.

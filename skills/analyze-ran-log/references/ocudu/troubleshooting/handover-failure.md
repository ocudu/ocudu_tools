# Troubleshooting: handover failure

A handover was triggered but did not complete — no `rrcReconfigurationComplete`
on the target, or an RLF / reestablishment after `reconfigurationWithSync`.

For the **expected sequences** (intra-gNB inter-cell and inter-gNB), the Retina
test catalogue, and the scheduler-only visibility notes, see
`../reference/handover.md`.

## Failure markers

| Marker | Likely cause |
|---|---|
| HO command sent but no `rrcReconfigurationComplete` (target side) | UE failed RACH on the target / RLF |
| `rrcReestablishmentRequest` shortly after HO command | Failed sync → falling back to reestablishment (see `reestablishment.md`) |
| `Late DL/UL HARQs` spike on the target around HO time | Target cell radio conditions / TA outdated |
| HO command never sent though `trigger_handover_from_measurements: true` and meas report received | Measurement event A3 threshold not met, or no neighbour cell config |

## Investigation checklist

1. Are RRC/CU layers visible? Check log levels (a layer without its own key takes `all_level`):
   ```bash
   grep -E "all_level|rrc_level|cu_level" ocudu_*.yml
   ```
   If RRC and CU resolve to `warning`, skip directly to step 4 (pcap) — the gNB log alone
   won't show the HO command body.
2. Count handovers completed (info; intra-CU by the target routine, NGAP by
   the target's `HandoverNotify`) and attempted (NGAP `HandoverRequired` on
   the source):
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/ocudu/ocudu_log_search.py gnb.log --layer CU-CP --pattern 'Handover Target Routine" finished' --count
   python3 ${CLAUDE_SKILL_DIR}/scripts/ocudu/ocudu_log_search.py gnb.log --layer NGAP --pattern 'Tx PDU.*HandoverNotify' --count
   python3 ${CLAUDE_SKILL_DIR}/scripts/ocudu/ocudu_log_search.py gnb.log --layer NGAP --pattern 'Tx PDU.*HandoverRequired' --count
   ```
   Intra-CU triggers minus target finishes = intra-CU HOs that did not complete:
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/ocudu/ocudu_log_search.py gnb.log --layer CU-CP --pattern 'Trigger intra-CU' --count
   ```
   `reconfigurationWithSync` (`--layer RRC --pattern reconfigurationWithSync`)
   is in RRC bodies at `rrc_level: debug` only.
3. Per-UE HO timeline (info):
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/ocudu/ocudu_log_search.py gnb.log --pattern 'Trigger .*handover|Handover.*(Routine|Procedure)' --max-lines 30
   ```
4. Use the F1AP/NGAP PCAP for the HO command body — handoff to the `pcap` type:
   - F1AP HO trace: UEContextModificationRequest with `reconfigurationWithSync`.
   - NGAP HO trace (inter-gNB): HandoverRequired / HandoverCommand.
5. If scheduler events show ue_reconf without RRC trace, infer the HO from the
   target cell's PRACH on a new pci:
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/ocudu/ocudu_log_search.py gnb.log --layer SCHED --pattern "prach\(" --max-lines 30
   ```
6. Cross-correlate with the Amarisoft UE log (the `amari-ue` type) — the
   UE log makes it obvious whether the UE acquired the target cell, sent the
   reconfigComplete, or fell into RLF.

## Cross-references

- `../reference/handover.md` — the expected HO sequences and test catalogue.
- `reestablishment.md` — HO failure usually surfaces as a reestablishment.
- `pcap` type: F1AP / NGAP / RRC handover messages.

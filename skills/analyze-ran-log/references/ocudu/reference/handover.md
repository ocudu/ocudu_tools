# Procedure: Handover

gNB-log observation surface. The artifact-agnostic message ladder + variants is
`../../common/procedures/handover.md`; this file maps it to the OCUDU `gnb.log`
lines. For a HO that was triggered but did not complete, diagnose with
`../troubleshooting/handover-failure.md`.

NR handovers come in two flavours in OCUDU runs:

- **Intra-gNB / inter-cell within the same DU** — the source and target cells
  are both managed by the same gNB. The HO command is delivered as an
  `rrcReconfiguration` whose body contains the `reconfigurationWithSync` IE
  (with the target cell's pci, RACH config, and security key chain info).
  No NGAP traffic is involved.
- **Inter-gNB (XnAP or NGAP-based)** — visible as NGAP `HandoverRequired` /
  `HandoverRequest` / `HandoverCommand` / `HandoverNotify` on the gNB log,
  plus an Xn association if XnAP is used.

In the Retina test catalogue:
- `mobility.intra_ru_ho.key_regen` — intra-gNB HO between cells served by the
  same RU.
- `mobility.inter_ru_ho.cfra_ho` — intra-gNB HO between cells on different RUs
  (still same gNB), exercising Contention-Free Random Access on the target.
- `mobility.reestablishment.intra_ru` — RLF + RRC reestablishment (see
  `reestablishment.md`).

These three tests typically have `rrc_level: warning` and `cu_level: warning`,
so the HO command itself **does not appear** in `gnb.log`. The visible signals
are at the scheduler/MAC level. To see the HO command body, enable
`f1ap.pcap` (already enabled in these tests) and use the `pcap` type.

## Expected sequence — intra-gNB inter-cell HO (info-level RRC enabled)

| Step | Layer | Log line |
|---|---|---|
| 1 | CU-CP | `ue=N: Trigger intra-CU (intra-DU) handover on du=N` or `Trigger intra-CU (inter-DU) handover from source_du=N to target_du=N` |
| 2 | RRC  | `DCCH DL rrcReconfiguration` (source UE); at `rrc_level: debug` its body holds `reconfigurationWithSync` |
| 3 | CU-CP-F1 | `Tx PDU ... UEContextModificationRequest` to the DU (target cell config) |
| 4 | DU-F1 | `Rx/Tx ... UEContextModificationResponse` |
| 5 | SCHED | New `prach(...)` on the target pci with the new `tc-rnti` (CFRA preamble in CFRA tests) |
| 6 | MAC  | `proc="MAC UE Reconfiguration": finished successfully` on the target pci |
| 7 | RRC  | `DCCH UL rrcReconfigurationComplete` on the target cell |
| 8 | CU-CP | `ue=N: "Intra CU Handover Target Routine" finished successfully` (target UE; `started...` at step 2). The source side, `"Intra CU Handover Routine"`, logs at debug only |

Scheduler-only visibility (when RRC/CU layers are at warning):
- `[METRICS] events=[..., {rnti=0xN slot=N.N type=ue_reconf}, ...]` —
  a `ue_reconf` event near the handover time.
- `[SCHED] Cell creation idx=N` for both cells at startup.
- New PRACH events on the target cell after the HO command.

## Expected sequence — inter-gNB HO (source side)

| Step | Layer | Log line |
|---|---|---|
| 1 | RRC | Measurement report decoded |
| 2 | NGAP | `ue=N ran_ue=N amf_ue=N: "Handover Preparation Procedure" started...` |
| 3 | NGAP | `Tx PDU ue=N ran_ue=N: HandoverRequired` |
| 4 | NGAP | `Rx PDU ... HandoverCommand` |
| 5 | RRC | `DCCH DL rrcReconfiguration` (carrying the target's `reconfigurationWithSync`) |
| 6 | NGAP | `Tx PDU ... UplinkRanStatusTransfer` (source→AMF; the target receives the matching `DownlinkRANStatusTransfer`) |
| 7 | CU-CP | UEContextRelease after the handover completes on the target |

Target side: NGAP `Rx PDU amf_ue=N: HandoverRequest`, `Tx PDU ...: HandoverRequestAcknowledge`, then
`Tx PDU: HandoverNotify` once the UE completes the handover.

## Diagnosing failures

For failure markers and the investigation checklist (including the
RRC/CU-at-warning → pcap path), see `../troubleshooting/handover-failure.md`.

## Cross-references

- `reestablishment.md` — HO failure usually surfaces as a reestablishment.
- `pcap` type: F1AP / NGAP / RRC handover messages.

# Procedure: PDU session / DRB setup

gNB-log observation surface. The artifact-agnostic message ladder is
`../../common/procedures/pdu-session-setup.md`; this file maps it to the OCUDU
`gnb.log` lines. For an attach that succeeded at RRC but shows **no data flow**,
diagnose with `../troubleshooting/no-user-plane.md`. Setup overlaps Initial
Context Setup — see `ue-attach.md`.

## Expected sequence

| Step | Layer | Log line |
|---|---|---|
| 1 | NGAP | `Rx PDU ... InitialContextSetupRequest` (contains PDU session list) |
| 2 | CU-CP-E1 | `Tx PDU ... BearerContextSetupRequest` |
| 3 | CU-UP-E1 | `Rx PDU ... BearerContextSetupRequest` |
| 4 | CU-UP | `ue=N: UE created` (the bearer context; the PDU session setup itself logs only at debug: `PDU session uplink tunnel info: psi=N local_teid=... peer_teid=... peer_addr=...`) |
| 5 | CU-UP-E1 | `Tx PDU ... BearerContextSetupResponse` |
| 6 | CU-CP-E1 | `Rx PDU ... BearerContextSetupResponse` |
| 7 | CU-CP-F1 | `Tx PDU ... UEContextModificationRequest` (registers the DRBs at the DU; the DU returns its F1-U DL TEIDs) |
| 8 | CU-CP-E1 | `Tx PDU ... BearerContextModificationRequest` (with DRB DL/UL teids) |
| 9 | CU-UP-E1 | `Tx PDU ... BearerContextModificationResponse` |
| 10 | CU-UP | `ue=N: Attaching dl_teid={Addr=... TEID=0x...} to F1-U tunnel with ul_teid=0x...`, then `ue=N: Modified DRB1. psi=1 f1u_teid=0x...` |
| 11 | RRC  | `DCCH DL rrcReconfiguration` (carries DRB config, RLC bearer config, PDCP) |
| 12 | RRC  | `DCCH UL rrcReconfigurationComplete` |
| 13 | GTPU | `Tunnel added. teid=0xNNNNNN` (one per direction) |

After step 13 the user-plane is up. The first DL packet from the core appears
as `[GTPU] [I] ue=N DL teid=0x...: RX SDU. sdu_len=N qos_flow=QFI=N`.

## Diagnosing failures

For failure markers and the investigation checklist, see
`../troubleshooting/no-user-plane.md`.

## Cross-references

- `ue-attach.md` — bearer setup overlaps with the attach procedure.
- `pcap` type: `e1ap.pcap`, `f1ap.pcap` carry the full IE bodies.

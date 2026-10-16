# Troubleshooting: Handover (HO)

A handover fails across the pcaps — the target never sets up context, the CFRA
RAR is missing, or the UE re-establishes/releases instead of completing on the
target. For the **expected across-pcaps sequence** and the HO variants this walks
against, see `../reference/handover.md`.

## Failure markers

| Symptom | Cause hypothesis |
|---|---|
| No `UEContextSetupRequest` on target | HO not triggered or CU never decided to hand over. |
| `UEContextSetupFailure` on target | Target DU can't admit — resources, cell config, S-NSSAI. |
| No CFRA RAR on target after target context setup | Target PRACH not configured for CFRA, or UE never sent the preamble (logs). |
| No `RRCReconfigComplete` after RAR | UE failed on target; expect re-establishment or release. |
| Re-establishment after HO (a new `InitialULRRCMessageTransfer` for the same UE after the HO window, carrying an `RRCReestablishmentRequest` inside the RRC container) | HO failed; the UE is recovering. The RRC `cause` IE lives inside the embedded RRC message, not as an F1AP field. |
| Inter-CU: `HandoverFailure` (NGAP) | Target rejected — check NGAP cause IE. |

## tshark filters

```bash
# Inter-CU HO triggers (HandoverPreparation / HandoverResourceAllocation)
tshark -r ngap.pcap -Y 'ngap.procedureCode == 12 || ngap.procedureCode == 13'

# The F1AP ids and C-RNTI of each context of the UE, source and target, from its RAN UE NGAP id
python3 ${CLAUDE_SKILL_DIR}/scripts/correlate/ue_table.py <run-dir> --where ran_ngap=<id>

# Its messages in 1 s around the HO; --ue matches the id in every protocol, so keep one protocol per id
python3 ${CLAUDE_SKILL_DIR}/scripts/pcap/correlate_run.py <run-dir> \
    --protocols ngap --ue <ran-ngap-id> --around <epoch> --window-ms 1000
python3 ${CLAUDE_SKILL_DIR}/scripts/pcap/correlate_run.py <run-dir> \
    --protocols f1ap --ue <du-f1ap-id> --around <epoch> --window-ms 1000
```

`--window-ms` applies only with `--around`.

## Cross-references

- `../reference/handover.md` — the expected HO sequence + variants this walks against.
- `../reference/protocols/ngap.md`, `../reference/protocols/f1ap.md`, `../reference/protocols/mac.md`
- `../reference/cross-pcap-correlation.md`

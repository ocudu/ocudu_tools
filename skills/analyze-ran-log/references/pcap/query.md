# pcap — query slot

Type-specific slot for the **query** activity; the driving playbook pulls this
in.

## restate and scope

Identify the smallest filter that answers the question: which pcap, which display
filter, which fields. If a multi-UE capture and the question pins no UE, clarify
scope before running broad queries (candidate UEs: `f1ap_ue_ids.py` /
`ngap_ue_ids.py` / `e1ap_ue_ids.py`).

## execute

Use a helper script first when one fits. They live in
`${CLAUDE_SKILL_DIR}/scripts/pcap/` and are run with `python3 <full path>`:

| Question | Command |
|---|---|
| "What NGAP procedures did UE X go through?" | `ngap_procedures.py <ngap.pcap> --ue <ran_ue_id>` |
| "How many F1AP / NGAP / E1AP messages of each type?" | `extract_proc_codes.py <pcap> --proto <ngap\|f1ap\|e1ap>` |
| "What happened around epoch T across all 5 pcaps?" | `correlate_run.py <run-dir> --around <epoch> --window-ms 2000` |
| "Which F1AP / NGAP / E1AP UEs are in this capture?" | `f1ap_ue_ids.py <f1ap.pcap>` (likewise `ngap_ue_ids.py` / `e1ap_ue_ids.py`, each on its own protocol pcap) |
| "What F1AP messages did UE X exchange?" | `f1ap_messages.py <f1ap.pcap> --ue <cu or du F1AP id>` or `--rnti <C-RNTI>` (4601, 0x4601 or 17921), which keeps every message of the UE contexts of that C-RNTI, up to their release; `f1ap_ue_ids.py` takes the same filters |

Otherwise hand-craft a minimal `tshark` filter:

```bash
tshark -r <file.pcap> \
  -Y '<display filter>' \
  -T fields -t ad -E separator=$'\t' \
  -e frame.number -e frame.time_epoch -e <protocol fields…> \
  | head -n 200
```

If the result exceeds 200 rows, narrow further (add a
`frame.time_epoch >= X && <= Y` clause, restrict to one UE, restrict to one
procedure code) or run it through a helper script that produces a compact summary.

Canonical filters and field names: `reference/tshark-recipes.md` and
`reference/protocols/<proto>.md`.

## answer notes

Cite **frame number(s)** and **epoch timestamp(s)** plus the exact tshark filter
used. Where relevant, name the corresponding event in a sibling pcap — e.g. an
NGAP `InitialContextSetupRequest` at epoch T paired with the F1AP `UEContextSetup`
at T+δ.

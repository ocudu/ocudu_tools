# Procedure: PHY / radio link issues

The gNB log is a poor source for raw radio quality — most of that lives in
the UE log. The gNB log signals PHY problems indirectly: PRACH not progressing,
persistent `crc=KO` on PUSCH, late HARQs, ZMQ "Waiting for data" stalls
(simulator only), and metrics rows showing degraded SNR.

## Failure markers

| Marker | Meaning |
|---|---|
| `[PHY     ] [I] [   N.N] PRACH: rsi=N rssi=N detected_preambles=[]` | PRACH occasion but no preamble decoded (UE didn't send, or power too low) |
| `[PHY     ] [I] ... PUSCH: rnti=0x... crc=KO ... sinr=N.NdB` | UL block didn't decode — possible UE TX issue or radio degradation |
| Many `[PHY     ] [I] ... PUSCH: ... sinr=-infdB` | UL silence (no UE TX during the slot) — only a problem when expected to be active |
| `[SCHED   ] [W] [...] Late DL HARQ ACK` | DL HARQ feedback didn't arrive in time → throughput regression |
| `[SCHED   ] [W] [...] Late UL HARQ` | UL HARQ ack window missed |
| `[zmq:rx:0:0] [I] Waiting for data.` after a UE released | Normal idle in simulator; persistent before any UE attached means the UE side didn't start |
| `[METRICS ] ... msg3_nok=N (N > 0)` | MSG3 decode failures — RACH initial UL attempt failed |
| `[METRICS ] ... failed_dl_pdcch=N` / `failed_ul_pdcch=N` (older builds: `failed_pdcch=N`) | Scheduler couldn't place a PDCCH in time; grep `failed_(common_)?(dl_\|ul_)?pdcch=[1-9]` |

## Investigation checklist

1. Are PRACH events progressing to UE creations? Count PRACHs with `grep -o`: one SCHED line can list
   several (`prach(...)` at info, `- PRACH: slot=` lines at debug). DU-MNG logs one `UE Create` per DU
   UE; `UE created` matches CU-CP and CU-UP lines, so it overcounts.
   ```bash
   grep -oE 'prach\(ra-rnti|- PRACH: slot=' gnb.log | wc -l
   python3 ${CLAUDE_SKILL_DIR}/scripts/ocudu/ocudu_log_search.py gnb.log --layer DU-MNG --pattern 'proc="UE Create": Procedure finished' --count
   ```
   If PRACH count > UE creations by a lot, MSG3 / MSG4 are failing — check
   `crc=KO` and `msg3_nok` in the metrics rows. UEs a handover creates on a target DU also count as
   UE creations.
2. CRC failure timeline (cap at 100):
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/ocudu/ocudu_log_search.py gnb.log --layer PHY --pattern "crc=KO" --max-lines 100
   ```
3. Late HARQs from metrics:
   ```bash
   python3 -c "
   import json
   recs = json.load(open('metrics.json'))   # metrics.json is a JSON array
   late_dl = sum((c.get('cell_metrics',{}).get('late_dl_harqs',0) or 0)
                 for r in recs if 'cells' in r for c in r['cells'])
   late_ul = sum((c.get('cell_metrics',{}).get('late_ul_harqs',0) or 0)
                 for r in recs if 'cells' in r for c in r['cells'])
   print(f'total late DL/UL HARQs: {late_dl}/{late_ul}')
   "
   ```
4. ZMQ stall (simulator only):
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/ocudu/ocudu_log_search.py gnb.log --pattern "Waiting for data" --max-lines 5
   ```
   If this appears before any UE attached and persists, the simulator's UE
   side is not producing samples — check the `amarisoft-ue-*` component.
5. Switch to the UE log for radio-side ground truth via
   the `amari-ue` type — the UE log records PRACH transmissions, CSI
   reports, and RLF flags.

## Cross-references

- `throughput-degradation.md` — what late HARQs and failed PDCCH
  do to throughput.
- UE log: handoff to the `amari-ue` type for cell search, PRACH TX, RLF.

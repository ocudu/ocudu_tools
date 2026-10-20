# Troubleshooting: UE attach failure

The UE never completes RRC connection + Initial Context Setup — no `UE created`,
no `rrcSetupComplete`, or the `"Initial Context Setup Routine"` never reaches
`finished successfully`.

For the **expected message sequence** (the 31 steps this walks against), see
`../reference/ue-attach.md`. This doc is the failure dispatch: find the last step
that happened, then the first that didn't.

## Boundary vs. fallback scheduling

A missing `rrcSetupComplete` has two distinct causes — split on whether Msg4 was
ever emitted:

- **MSG4 / ConRes never *scheduled*** (no `CON_RES` DL PDU; `ra-ContentionResolutionTimer`
  expires) → the fallback scheduler never got the grant out → **not here**, go to
  `ue-fallback-scheduling-issues.md`.
- **MSG4 was scheduled + ACKed but the UE never replied**, or the attach broke at
  PRACH / NGAP-auth / security / ICS → **this doc**.

## Failure markers

| Where it fails | Marker | Likely cause |
|---|---|---|
| Before step 1 | PRACH not detected at all | UE TX power / SSB alignment / wrong `prach_config_index` → `phy-issues.md` |
| Step 1 → 6 | MSG3 doesn't decode (`crc=KO` on PUSCH for that rnti) | Power / timing / wrong MSG3 grant params → `phy-issues.md` |
| Step 6 missing (MSG4/ConRes never scheduled) | UE created but no `CON_RES` DL PDU; `ra-ContentionResolutionTimer` expires | Fallback scheduling → `ue-fallback-scheduling-issues.md` |
| Step 8 missing | `InitialULRRCMessageTransfer` arrived but `UE created` not logged | CU-CP rejected the UE — check warnings; could be `max_nof_ues` hit |
| Step 11 missing (but Msg4 was sent) | `rrcSetupComplete` never seen | UE didn't decode/ACK Msg4, or NAS PDU encoding failed (UE/UL side) |
| Step 11 missing, **2-step RA** (`MsgB: ... tbs=12` in `[SCHED]`, not a 4-step `RAR`) | UE ACKed MsgB/successRAR (PUCCH `ack=1`) but the follow-up PDSCH under the new `c-rnti` (carrying `RRCSetup`) is HARQ-retransmitted and discarded, never ACKed | See `../../common/procedures/random-access.md` § 2-step RA type before concluding anything — `S=0` (successRAR without piggybacked RRC) is a spec-legal MsgB format, **not** itself the bug; check whether the UE/test-rig resumes PDCCH monitoring under the fresh C-RNTI (UE/test-rig-side candidate) vs. the gNB's search-space config for that C-RNTI (gNB-side candidate) |
| Step 15 missing | NAS exchange stalls between RRC and `InitialContextSetupRequest` | AMF auth failure (check `amf_ue` ID transition), 5GC issue |
| Step 30 missing | `"Initial Context Setup Routine"` logged `initialized` but never `finished successfully` | UE didn't reply to securityMode / Reconfiguration; or bearer setup failed in E1AP → `no-user-plane.md` |
| Step 31 then immediate UEContextRelease from AMF | NAS rejected the request | Check `cause` IE in `UEContextReleaseCommand` → `ue-release-issues.md` |

## Investigation checklist

1. Did PRACH happen?
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/ocudu/ocudu_log_search.py gnb.log --layer SCHED --pattern "prach\(" --count
   ```
2. Did the UE get a C-RNTI?
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/ocudu/ocudu_log_search.py gnb.log --pattern "UE created" --max-lines 20
   ```
3. Did rrcSetupComplete arrive?
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/ocudu/ocudu_log_search.py gnb.log --pattern "rrcSetupComplete" --max-lines 10
   ```
4. Was Initial Context Setup acknowledged?
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/ocudu/ocudu_log_search.py gnb.log --pattern '"Initial Context Setup Routine"' --max-lines 20
   ```
5. If a specific UE failed, scope everything by `c-rnti` and look at the gap:
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/ocudu/ocudu_log_search.py gnb.log --rnti <hex> --max-lines 200
   ```
6. Cross-correlate with the Amarisoft UE log via the `amari-ue` type — the
   UE log shows whether the UE actually decoded MSG4, whether it sent
   Reconfiguration Complete, and whether NAS Auth Response went out.

## Multi-UE specifics

In `multiue.attach_dettach.baseline` the gNB processes UEs in batches. The
`[SCHED] [W] UE creation (ue=N): latency1=...` and
`[MAC] [W] MAC UE creation (ue=N): ...` warnings are **expected diagnostic
output**, not real warnings — they report per-UE creation latency for
performance tracking.

When `Initial Context Setup OK : K/N` shows K < N, the gap is the most
useful single signal. Find which UEs missed it, one `<creation ts> ue=N` line per
CU-CP UE context whose `ue=N` was created again, or the log ended, before its
ICS finished. `ue=` indexes are reused, so compare per creation, not per index;
the CU-UP also logs `ue=N: UE created`, hence the `CU-CP` anchor:

```bash
C="${CLAUDE_CODE_TMPDIR:-/tmp}/claude-skills-${CLAUDE_CODE_SESSION_ID}"; mkdir -p "$C"
LC_ALL=C grep -aE '\[CU-CP +\] \[I\] ue=[0-9]+( c-rnti=0x[0-9a-f]+: UE created|: "Initial Context Setup Routine" finished)' gnb.log \
  | awk '{ match($0, /ue=[0-9]+/); u = substr($0, RSTART, RLENGTH) }
         /UE created/ { if (u in p) print p[u]; p[u] = $1 " " u; next }
         { delete p[u] } END { for (u in p) print p[u] }' | sort > "$C/ocudu-missed-ics.txt"
wc -l < "$C/ocudu-missed-ics.txt"
```

Do not build this from `ocudu_log_search.py`: it stops at its `--max-lines` cap
(default 200) and prints its `... N more` note to stdout.

## Cross-references

- `../reference/ue-attach.md` — the expected attach sequence this walks against.
- `ue-fallback-scheduling-issues.md` — MSG4/ConRes never scheduled (step 6).
- `no-user-plane.md` — attach reached RRC but DRBs/data never came up.
- `phy-issues.md` — PRACH / MSG3 radio-side failures.
- `../../common/procedures/random-access.md` § 2-step RA type — MsgA/MsgB ladder
  and the two spec-legal MsgB formats.

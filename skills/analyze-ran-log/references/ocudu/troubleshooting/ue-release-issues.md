# Troubleshooting: UE release issues

A UE released unexpectedly, or a release started but never completed (context
leak), or releases produced a burst of DL KOs. The trigger is normally NGAP
`UEContextReleaseCommand`, propagating down E1AP (bearer) and F1AP (UE context).

For the **expected release sequence** (19 steps) and why the un-ACKed RRCRelease
at step 11 is usually benign, see `../reference/ue-release.md`.

## Failure markers

| Marker | Meaning |
|---|---|
| Release triggered by gNB inactivity (`Inactivity timer expired`) | Normal in idle tests — UE was attached but didn't generate traffic for `cu_cp.inactivity_timer` seconds |
| `BearerContextReleaseComplete` missing | CU-UP didn't ack — process stuck (check `[CU-UP   ]` lines) |
| `"UE Removal Routine" finished successfully` missing despite step 1 happening | Release got stuck mid-flow; CU-CP context leak — check warnings for F1/E1 timeouts |
| `RRC container not ACKed within a time window of 120msec` | Benign in simulator runs (the UE may have already detached); becomes a concern on real radios if persistent |
| Burst of DL KOs in a UE's final metrics interval | The un-ACKed RRCRelease + in-flight DL HARQs retransmit through all RVs — a UL scheduling race under load, not a link fault → `harq-ko-bler.md` |

## Investigation checklist

1. Find release triggers:
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/ocudu/ocudu_log_search.py gnb.log --pattern "UEContextReleaseCommand" --max-lines 20
   ```
2. Match each command to its completion:
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/ocudu/ocudu_log_search.py gnb.log --pattern "UE Removal Routine.*finished" --count
   ```
3. Was the cause IE in the command an error? The cause is carried in the
   NGAP body — visible in `ngap.pcap` (handoff to the `pcap` type), or in
   `gnb.log` only when `ngap_level: info` and `hex_max_size > 0`.
4. For UEs that never released (creations > removals), list each CU-CP UE
   context (`<creation ts> ue=N`) whose `ue=N` was created again, or the log
   ended, before its removal finished. `ue=` indexes are reused, so compare per
   creation; the CU-UP also logs `ue=N: UE created`, hence the `CU-CP` anchor:
   ```bash
   LC_ALL=C grep -aE '\[CU-CP +\] \[I\] ue=[0-9]+( c-rnti=0x[0-9a-f]+: UE created|: "UE Removal Routine" finished)' gnb.log \
     | awk '{ match($0, /ue=[0-9]+/); u = substr($0, RSTART, RLENGTH) }
            /UE created/ { if (u in p) print p[u]; p[u] = $1 " " u; next }
            { delete p[u] } END { for (u in p) print p[u] }' | sort
   ```

## Cross-references

- `../reference/ue-release.md` — the expected release sequence.
- `harq-ko-bler.md` — release-phase DL KO bursts (the load-dependent feedback race).
- `pcap` type: `ngap.pcap` carries the `cause` IE in `UEContextReleaseCommand`.

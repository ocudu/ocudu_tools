# VIAVI — query slot

Type-specific slot for the **query** activity; the driving playbook pulls this
in.

## restate and scope

Identify the `UE Id` (decimal) / event / time window to scope by.

Candidate-listing recipes when scope is ambiguous — the summary script already
names the UE Ids behind RA errors and connection failures, so prefer its Anomalies
block; otherwise:

```bash
L=<command-log>
LC_ALL=C grep -ao 'UE Id:[0-9]*' "$L" | sort -u | head -n 40   # all UE Ids seen
LC_ALL=C grep -a 'Random Access Error' "$L" | head -n 40        # the failing ones
```

## execute

Use the search script first when it fits:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/viavi/viavi_log_search.py <command-log> \
  [--ue <id>] \
  [--event <substr>] \
  [--after <HH:MM:SS:mmm>] \
  [--before <HH:MM:SS:mmm>] \
  [--pattern <regex>] \
  [--count] \
  [--max-lines 200]
```

| Question | Command |
|---|---|
| "How many registration events?" | `--event "NR REGISTRATION IND" --count` (events, not UEs: a UE registers again after each reconnect) |
| "Did any random access fail?" | `--event "Random Access Error" --count` |
| "Show everything for UE 113" | `--ue 113` |
| "When did UE 113 first connect?" | `--ue 113 --event "NR CONNECTION IND"` |
| "Any connection failures?" | `--event "NR CONNECTION FAILED IND"` |
| "Handovers in a time window" | `--event "Handover Complete" --after … --before …` |
| "Did the RUs come up?" | `--pattern "CU PLANE ACTIVE"` |
| "RLC retransmission limit hit?" | `--pattern "maximum retransmissions"` |
| "Any failed tester commands?" | `--pattern "C: [A-Z_]+ 0x(?!00)"` |

`--event` is a **substring** filter on `I: CMPI` lines — use the precise form
(`"NR REGISTRATION IND"`, not `"REGISTRATION IND"`, which also matches
`DEREGISTRATION IND`).

`--ue N` matches `UE Id:N` / `UE ID: N`, not a COMBINED GETSTATS range
(`UE ID: 0-32`). `--after`/`--before` take each time at its first occurrence at or
after the first log line, so a window across midnight works.

Distinct UEs that registered (the count of events above overstates it):

```bash
LC_ALL=C grep -a 'NR REGISTRATION IND' "$L" | grep -oE 'UE Id: ?[0-9]+' | tr -d ' ' | sort -u | wc -l
```

Otherwise use the canonical greps in `reference/log-format.md` § Key grep recipes;
cap with `| head -n 200` and spill larger results to `viavi-query-<sha>.txt`.

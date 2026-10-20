# OCUDU — query slot

Type-specific slot for the **query** activity; the driving playbook pulls this
in.

## restate and scope

Identify:

- Which file(s) to search (`gnb.log`, `stdout.log`, `ocudu_gnb.yml`,
  `metrics.json`).
- Which grep pattern or script flag answers it.
- Whether to scope by UE (`ue=N` on the CU side, `c-rnti=0xNNNN` on the DU side)
  or by cell (`pci=N`).

Candidate-listing recipes when scope is ambiguous:

```bash
grep -oE 'ue=[0-9]+ c-rnti=0x[0-9a-f]{4}: UE created' gnb.log | sort -u   # UEs
grep -oE 'pci=[0-9]+' gnb.log | sort -u                                   # cells
```

## execute

Use the search script first when it fits:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/ocudu/ocudu_log_search.py <gnb.log> \
  [--layer <exact tag>] \
  [--ue <N>] \
  [--rnti <RNTI>] \
  [--pci <N>] \
  [--after <HH:MM:SS.mmm>] \
  [--before <HH:MM:SS.mmm>] \
  [--pattern <regex>] \
  [--level <regex over D|I|W|E>] \
  [--count] \
  [--max-lines 200]
```

`--rnti` and `--where rnti=` of every script take any form: `4601` (hex, as logs
print it), `0x4601`, or `17921` (decimal, as F1AP pcaps hold it). Exception:
`correlate_run.py --ue` takes a protocol UE id as the pcap holds it, an RNTI only as
`0x4601` or decimal `17921`; a bare `4601` is decimal there. `--after`/`--before` take a time of day
(`13:35:40.5`), a date, or a date and time joined by `T` or a space, compared to
the same precision.

`--layer` is an **exact** tag match, not a substring — pass a tag that really
appears in the log. There is **no `F1AP` tag**, and F1AP/E1AP PDUs are logged by
endpoint (`CU-CP-F1`, `DU-F1`, `CU-CP-E1`, `CU-UP-E1`); `E1AP` only tags E1AP
ASN.1 conversion messages. Tags of the OCUDU loggers (`reference/log-format.md`
§ Layer tags), present only when their layer logs:

```
ALL APP CONFIG GNB CU DU EAL METRICS
RRC NGAP XNAP NRPPA E1AP E2AP E2SM-KPM E2SM-RC E2SM-CCC ASN1 F1AP-ASN1-PCK E1AP-ASN1-PCK
CU-CP CU-CP-F1 CU-CP-E1 CU-CP-XN CU-UEMNG E2-CU-CP
CU-UP CU-UP-E1 CU-F1-U F1-U GTPU PDCP SDAP ROHC SEC E2-CU-UP
DU DU-MNG DU-F1 DU-F1-U MAC SCHED RLC FAPI PHY OFH RU RF HWACC NTN E2-DU
SCTP-GW SCTP UDP-GW IO-EPOLL E2-SUBSCRIBER
zmq:rx:<stream>:<ch> zmq:tx:<stream>:<ch>
```

List the tags of one log with
`LC_ALL=C grep -aoE '^\S+ \[[^]]+\]' gnb.log | sed -E 's/.*\[ *//; s/ *\]$//' | sort | uniq -c`.

| Question | Command |
|---|---|
| "How many handovers?" | `--layer CU-CP --pattern 'Handover Target Routine" finished' --count` (intra-CU); `--layer NGAP --pattern 'Tx PDU.*HandoverNotify' --count` (NGAP, target) |
| "When did the UE attach?" | `--layer RRC --pattern "DCCH UL rrcSetupComplete"` |
| "All PRACH events" | `--layer SCHED --pattern "prach\("` |
| "Cells configured?" | `--pattern "Cell creation idx="` |
| "Final RRC release?" | `--layer RRC --pattern "rrcRelease"` |
| "Bearer setups?" | `--pattern "BearerContextSetupResponse" --count` |
| "Which band/BW used?" | read `ocudu_gnb.yml` or `grep "^Cell pci=" stdout.log` |
| "Did the UE complete attach?" | `--layer CU-CP --pattern '"Initial Context Setup Routine" finished'` |
| "Reestablishment seen?" | `--pattern "rrcReestablishment"` |
| "UE's first RRC msg across F1?" | `--layer CU-CP-F1 --pattern "InitialULRRCMessageTransfer"` |

Regex alternation can't be written inside a markdown table cell, so these live
here — copy them verbatim, the pipe must **not** be backslash-escaped:

```bash
--pattern "NGSetupResponse|NGSetupFailure"              # NGAP connect to AMF
--pattern "Built in|Workers stopped successfully"       # run duration
--level "E|W|C"                                         # errors and warnings
```

Otherwise use targeted grep with the canonical recipes in
`reference/log-format.md` § Key grep recipes. Cap with `| head -n 200`; if larger,
narrow (time window, UE id, cell) or spill to `ocudu-query-<sha>.txt`.

**YAML/config questions** — look at **both** `ocudu_gnb.yml` (what the
user/Retina supplied; search top-to-bottom because of duplicate-key, last-wins
behaviour) **and** the `[CONFIG  ] [D] gNB input configuration` echo at the top of
`gnb.log` (the effective value after defaults and merges). Mention both if they
differ.

**Metrics questions** — for "max throughput", "BLER", "MCS of UE X", "any late
HARQs", query the `[METRICS]` lines of the log with the metrics script. Values are
normalized (times in `us`, bitrates in `bps`); it parses large logs in parallel:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/ocudu/ocudu_metrics.py <gnb.log>                     # layers and fields
python3 ${CLAUDE_SKILL_DIR}/scripts/ocudu/ocudu_metrics.py <gnb.log> --layer sched_ue \
  [--fields dl_brate,ul_brate,dl_mcs] [--by ue|rnti|pci] [--where rnti=0x4601] \
  [--after <HH:MM:SS>] [--before <HH:MM:SS>] [--rows] [-j <workers>]
```

Without `--rows` it prints count/min/mean/max per field, with `--rows` the matching
rows with their log line numbers. When the log has no `[METRICS]` lines, parse
`metrics.json` with python instead; never `cat` it whole. The summary script
already does the common rollups of it.

**Scheduler collisions** — for "does the scheduler allocate overlapping resources",
search the scheduler event traces (`schedtrace_cell<N>.bin`, written when the
`trace.schedtrace` option is on) for pairs of allocations of a slot that overlap:
PDSCH/PDSCH, PUSCH/PUSCH, PUSCH/PUCCH, PDCCH/PDSCH and PDCCH/PDCCH (shared CCEs):

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/ocudu/schedtrace_collisions.py <trace or run dir> \
  [--kinds PUSCH-PUCCH,PDCCH-PDCCH] [--rnti 0x4601] [--summary] [-j <workers>]
```

PDSCHs of DCI format 1_0 in a common search space (SIB, RAR, paging, fallback) are
placed from their CORESET, the others from the BWP. PDCCHs take the CRBs of their
CCEs in their CORESET.

**Scheduler per-UE metrics** — for "what did the scheduler do for UE X" at slot
resolution, e.g. throughput, MCS, PRBs, retransmissions, HARQ-ACK outcomes, CQI or
SRs between two times, read the scheduler event traces: a summary per UE, then one
row per period per UE, or the grants and reports themselves:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/ocudu/schedtrace_ue_metrics.py <trace or run dir> \
  [--rnti 0x4601] [--ue 0] [--after 13:35:40] [--before 13:35:41] [--period 0.01] \
  [--summary] [--grants dl|ul|harq|csi] [-j <workers>]
```

Fields follow the METRICS `Scheduler UE` lines where they mean the same: `dl_brate`
is of the ACKed TBS, `dl_mcs` the mean of the codewords and `cqi` of the reports, both
rounded half up as METRICS rounds them. Periods are bins
on whole multiples of `--period` since the epoch (UTC), while a METRICS period ends at
the time of its line, so a 1 s period overlaps two METRICS periods: compare trends,
not single values. Traces have no UL
CRC or buffer status, so UL throughput is of the scheduled new transmissions
(`ul_sched_brate`), and a DTX report counts as not ok.

## answer shape

```
**Answer:** The UE attached at 18:18:30.803 (gnb.log:758).

Evidence:
  18:18:30.803 [RRC] DCCH UL rrcSetupComplete           (line 758)
  18:18:30.952 [CU-CP] "Initial Context Setup Routine" finished successfully (line 890)

Commands used:
  grep -nE "DCCH UL rrcSetupComplete|Initial Context Setup Routine.*finished" gnb.log
```

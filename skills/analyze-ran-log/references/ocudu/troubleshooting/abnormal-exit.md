# Procedure: Abnormal exit

A healthy OCUDU gNB log ends with the file sink's closing line:

```
[GNB     ] [I] Stopping...
[GNB     ] [I] Closing PCAP files...                (info only)
[GNB     ] [I] PCAP files successfully closed.      (info only)
[ALL     ] [I] Stopping workers...                  (ALL logger at info only)
[ALL     ] [I] Workers stopped successfully.        (ALL logger at info only)
[METRICS ] [I] Stopped the executor metrics backend worker
(END)
```

`Stopping...` is always logged; the lines marked info only are absent at
warning, so their absence is no symptom. When the summary script reports
`Clean shutdown: no`, the log has no `(END)` (nor, for older builds without it,
`Workers stopped successfully` or `PCAP files successfully closed`).

## Likely modes

| Symptom | Mode |
|---|---|
| No `Stopping...`; last line mid-byte or `[APP] [E] Emergency flush of the logger` | Crash (SIGSEGV / abort) or kill during the run |
| `Stopping...` present, no `(END)` | Crash or hang during shutdown; the last lines name the stage (DU, CU-UP, CU-CP stop, PCAP close, workers when logged at info) |
| Process killed externally with SIGKILL | Same shape as a crash — no shutdown handler ran |
| Out-of-memory kill | dmesg / journalctl will show OOM; `ps_info_gnb.txt` may show RSS approaching the host limit |
| Watchdog / Retina timeout | `agent-log-*.log` in the parent dir shows `SIGTERM sent to gnb` followed by no exit — Retina then `SIGKILL`ed |

## Investigation checklist

1. Look at the last lines of `gnb.log`:
   ```bash
   tail -n 30 gnb.log
   ```
2. Look for `[E]` log lines just before the end:
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/ocudu/ocudu_log_search.py gnb.log --level E --max-lines 20
   ```
3. Check `agent-log-*.log` in the parent of the run dir — Retina records
   the SUT lifecycle:
   ```bash
   tail -n 40 ../agent-log-*.log
   ```
4. Check the process snapshot:
   ```bash
   head -n 5 ps_info_gnb.txt    # CPU / RSS at the time it was taken
   ```
5. Find the shutdown stage reached, from the lines after `Stopping...`:
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/ocudu/ocudu_log_search.py gnb.log --pattern "Stopping\.\.\.|stopped successfully|Stopping workers|Task worker .* finished" --max-lines 20
   ```
   Only a stage whose start line is logged and whose end line is missing shows
   a stuck stage; lines missing because their logger is below info show nothing.
6. If a core dump path is configured on the host, suggest the user inspect
   it (e.g. via `coredumpctl list` on systemd hosts).

## Cross-references

- `agent-log-*.log` files in the OCUDU component directory (one level up
  from the run dir) record SIGTERM/SIGKILL from Retina.
- `throughput-degradation.md` — if the gNB was wedged on
  scheduler latency before the crash, the metrics rows will show it.

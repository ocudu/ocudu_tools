# Troubleshooting: handover / reestablishment failure

A handover or RRC reestablishment did not go as expected. For the expected
handover and reestablishment sequences, see `../reference/handover.md`.

## Investigation checklist

### HO: expected but not seen

1. Check if HO was supposed to happen (config has multiple cells, or intra-RU HO test):
   ```bash
   grep "reconfigurationWithSync {" ue.log | wc -l
   grep -n "DCCH-NR: RRC reconfiguration" ue.log | wc -l
   ```
2. Count all reconfigurations vs. those with sync — if counts differ, some were
   bearer-only.
3. Check if RLF occurred instead of clean HO:
   ```bash
   grep -n "RRC reestablishment request\|crc=FAIL" ue.log | head -20
   ```

### HO: seen but UE lost connectivity (CBR loss spike)

1. Find exact HO timestamp:
   ```bash
   grep -n "reconfigurationWithSync {" ue.log | head -5
   ```
2. Check reconfiguration complete was sent (expected: ~10–30 ms later on target cell):
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/amari-ue/ue_log_search.py ue.log \
     --layer RRC --pattern "reconfiguration complete" --after <HO-time>
   ```
3. Check PRACH on target cell (CFRA/CBRA RA needed for HO):
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/amari-ue/ue_log_search.py ue.log \
     --layer PHY --pattern "PRACH:" --after <HO-time>
   ```
4. If PRACH retried many times → RA failure; if no PRACH → the UE likely used a
   preconfigured CFRA preamble (the gNB pre-assigns it), so a HO can complete with
   no visible PRACH line — that absence is normal, not a failure.

### Reestablishment: rejected or looping

1. Find reestablishment request and response:
   ```bash
   grep -n "reestablishment" ue.log | grep "\[RRC\]" | head -10
   ```
2. If followed by `RRC setup` (not `RRC reestablishment`) → gNB rejected,
   forcing full re-attach. Likely the UE's context was released on the gNB side.
3. If no response at all → gNB did not respond; check gNB logs.

## Cross-references

- ../reference/handover.md — the expected sequence this walks against.

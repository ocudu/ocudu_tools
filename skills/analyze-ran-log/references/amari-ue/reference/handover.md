# Handover and reestablishment procedures

UE-log observation surface. The artifact-agnostic ladders are
`../../common/procedures/handover.md` and
`../../common/procedures/reestablishment.md`; this file is the UE-side view. For
diagnosing a *failed* handover or reestablishment, see
`../troubleshooting/handover.md`.

## Handover (reconfigurationWithSync)

### Expected sequence (inter-cell HO, e.g. CL 00 → CL 01)

```
[RRC]   DL 0001 00 DCCH-NR: RRC reconfiguration
    ...
          reconfigurationWithSync {   ← this line is in the body, not the header
            ...
          }
[RRC]   UL 0001 01 DCCH-NR: RRC reconfiguration complete   ← note: cell 01 now!
```

Key indicator: the `RRC reconfiguration complete` is sent on the **target cell**
(different CL index than the `RRC reconfiguration`). In stdout.log the RNTI also
changes on the next stats row. In inter-RU handovers (two RF ports) the UE
alternates between CL 00 and CL 01, with a fresh RNTI on each HO.

### Grep for handovers

```bash
# Count handovers
grep -c "reconfigurationWithSync {" ue.log

# Find handover timestamps
grep -n "reconfigurationWithSync {" ue.log

# See which cell each reconfiguration complete was sent on
grep -n "DCCH-NR: RRC reconfiguration" ue.log
```

### Distinguishing HO from bearer-only reconfiguration

A `RRC reconfiguration` **without** `reconfigurationWithSync` in its body is a
bearer or measurement config update, not a handover. Confirm with:

```bash
# Get line numbers of all reconfiguration DL messages
grep -n "DL.*DCCH-NR: RRC reconfiguration$" ue.log

# For each line N, check lines N+1 through N+200 for reconfigurationWithSync
grep -A 200 "DL.*DCCH-NR: RRC reconfiguration$" ue.log | grep -m1 "reconfigurationWithSync {\|RRC reconfiguration complete"
```

A `reconfigurationWithSync` that carries a new `servingCellConfigCommon` block is a
full cell change (new frequency/band), not just a PCI or beam change.

---

## RRC Reestablishment

Triggered when the UE loses the serving cell (RLF). The UE sends a reestablishment
request on any cell it can reach.

### Expected sequence

```
[PHY]   DL 0001 00 ...   ← radio link failure (e.g. T310 expiry, many CRC FAIL)
[RRC]   UL 0001 00 CCCH-NR: RRC reestablishment request
[RRC]   DL 0001 00 DCCH-NR: RRC reestablishment    ← network accepts (DCCH/SRB1, not CCCH)
[RRC]   UL 0001 00 DCCH-NR: RRC reestablishment complete
[RRC]   DL 0001 00 DCCH-NR: RRC reconfiguration    ← restore bearers
[RRC]   UL 0001 00 DCCH-NR: RRC reconfiguration complete
```

If the network rejects the reestablishment request, it sends `RRC setup` instead,
forcing a full re-attach.

### Grep for reestablishments

```bash
# All reestablishment messages (request, reestablishment, complete)
grep -n "\[RRC\] .* RRC reestablishment" ue.log

# Count of attempts. A bare "reestablishment" also matches body lines
# (reestablishmentCause) and "reestablish ... without reconfigurationWithSync" notes.
grep -c "RRC reestablishment request" ue.log

# PHY failures before reestablishment (look for CRC FAIL spike)
grep -n "crc=FAIL" ue.log | head -20
```

---

## Diagnosing failures

For the investigation checklist (and failure markers), see
`../troubleshooting/handover.md`.

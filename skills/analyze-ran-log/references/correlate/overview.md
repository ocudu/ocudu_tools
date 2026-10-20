# correlate — overview slot

Type-specific slot for the **overview** activity; the driving playbook pulls this
in. It is the cross-source layer added **on top of** the per-component summaries,
and the value-add of a whole-run overview: things no single artifact can see.

Read `cross-correlation.md` first if not already loaded.

## clock / slot alignment

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/correlate/align_clocks.py <run-dir>
```

Checks log↔pcap (expected Δ≈0 — same process, so this is a sanity / display-TZ
guard rather than a measurement), UE↔gNB PHY slot alignment, and whether `slot_rx=`
is available. Surface a non-zero log↔pcap Δ or any PHY slot offset as an anomaly.

**Scope limit:** it does not check off-host sources (VIAVI tester, remote 5GC),
and check 2 needs a `ue.log` — for a gNB+VIAVI run it degrades to the one
same-process line. Report an off-host source as "offset not measured" rather than
implying it was verified; the `(SFN.slot, RNTI)` join doesn't depend on it.

## radio reconciliation (optional)

When the run has PHY-level data on both sides and the overview should state
whether the air interface was healthy:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/correlate/correlate_radio.py <run-dir> --kind pusch
```

Reports rx-ok / rx-ko / missing / contention. Skip for a quick overview; include
when the per-component summaries already flagged PHY anomalies. In a split run it
takes the first DU log and lists the other DUs; UE events on their cells read
`gnb-missing`, so rerun with `--gnb <other du.log>`. It exits with an error on a
gNB log without PHY lines (CU, or `phy_level` below info), and on a UE log without
them (empty, or PHY logging off in its `log_options`).

## cross-source block

Feeds the *Clocks*, *Cross-source picture* and *Anomalies* parts of the driving
playbook's consolidated block:

```
**Clocks:** all UTC; log↔pcap Δ <x> ms; UE↔gNB PHY slots aligned | offset <n> slots

### Cross-source picture
- Attach/release counts reconciled across UE ↔ gNB ↔ pcap
- Radio: <PUSCH rx-ok / rx-ko / contention, if correlate_radio.py was run>
- Timeline headline: first PRACH → attach → traffic → release
```

## cross-source anomalies

The ones worth calling out, because no single type can see them:

- UE reached REGISTERED but the gNB has no matching UE context (or vice-versa).
- pcap shows a release/cause the logs don't, or the counts disagree.
- gNB PUSCH `crc=KO` where the UE logged a transmission — a real decode issue, not
  UE DTX.
- Clock or PHY-slot misalignment reported by `align_clocks.py`.
- UE count vs gNB `UE created` count vs NGAP `InitialUEMessage` count disagree.
  For "how many UEs", use `amari-ue`'s cfg-derived count, **not** the testbed map's
  `amarisoft-ue-N` slot range (slots ≠ simulated UEs).

Count disagreement localises *where* UEs are lost — that pointer is the useful
output, not the raw tallies.

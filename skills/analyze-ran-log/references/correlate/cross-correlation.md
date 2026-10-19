# Cross-correlation reference (the master model)

How to line up the *same* event across the Amarisoft UE log, the OCUDU gNB log,
and the packet captures. Per-artifact parsing detail lives in the per-type
subtrees (`../pcap/`, `../ocudu/`, `../amari-ue/`); this file is only about
joining sources.

## Timestamp formats per source

| Source | Format | Example | Clock |
|---|---|---|---|
| `gnb.log` | ISO-8601, microseconds | `2026-04-29T14:27:21.265863` | UTC (container) |
| `ue.log` / `mme.log` line | `HH:MM:SS.mmm` (no date) | `14:27:21.273` | UTC (container) |
| `ue.log` / `mme.log` header | `# Started on YYYY-MM-DD HH:MM:SS` | `# Started on 2026-04-29 14:27:19` | UTC anchor (prepend the date to line clocks) |
| pcap | `frame.time_epoch` (UTC seconds) | `1777472837.801640` | UTC seconds |
| VIAVI `*_Command_Log*.txt` | `DD/MM/YY HH:MM:SS:mmm` | `18/06/26 12:05:34:913` | **tester-local — may differ from the gNB host** |

## Clocks: establish the offset per run — don't assume a shared wall-clock

- **Same process ⇒ Δ≈0 by construction.** One process emits `gnb.log` and its
  `gnb_*.pcap`, so they share a clock. Verified: gNB `NGSetupRequest` at
  `14:27:17.801593` = first NGAP frame epoch `1777472837.801640` (same instant).
- **ZMQ / co-located runs ⇒ UE and gNB share a clock too.** A ZMQ (simulated-RF)
  setup almost always runs the Amarisoft UE and the OCUDU gNB on one host, so
  their UTC container clocks align (Δ≈0); likewise any NTP-synced co-located
  containers. UE↔gNB wall-clock is comparable here — still allow for the small
  gNB decode-log latency (below), which is processing delay, not clock skew.
- **Off-host ⇒ may be offset seconds→hours.** Real-RF / split-host runs: a
  separate tester/UE box, a remote 5GC, an unsynced or other-TZ host. A VIAVI
  command log is **tester-local** (observed multi-hour offset; its window may not
  even overlap the gNB run).
- **So: prefer the clock-independent key `(SFN.slot, RNTI)` + RNTI/TC-RNTI chains
  (below).** Before any wall-clock compare against an off-host source, measure Δ
  from one uniquely-matched event (RNTI/TC-RNTI, NGAP setup); don't assume 0.
  Helper: `python3 ${CLAUDE_SKILL_DIR}/scripts/correlate/align_clocks.py <run-dir>`.
- **Display-TZ trap:** `capinfos`/`tshark` print frames in the host's local TZ (a
  CEST host shows `16:27:17` for the frame above). Use raw `frame.time_epoch`;
  treat container log strings as UTC. `utils.epoch_to_utc()` does it right.

## The exact radio key: PHY (SFN.slot, RNTI)

The UE PHY and the gNB PHY log the **same** PUSCH/PUCCH/PDCCH/PDSCH at the
**same SFN.slot and RNTI**. This is the precise, clock-independent join key.
Verified: UE `66.4 ... 4601 PUSCH` ↔ gNB `[66.4] PUSCH: rnti=0x4601` (and the
1080 PUSCH of a single-UE run pair 1:1).

```
ue.log : 14:27:21.273 [PHY] UL 0035 00 4601  123.14 PUSCH: harq=0 ... tb_len=11 ...
gnb.log: 2026-04-29T14:27:21.281595 [PHY] [I] [  123.14] PUSCH: rnti=0x4601 ... tbs=11 crc=KO sinr=97.0dB
         ^ same slot 123.14, same rnti 4601 ; wall-clock +8.6 ms = gNB decode-log latency (NOT clock skew)
```

Notes and caveats:
- **RNTI printing differs**: UE prints bare hex (`4601`), gNB prints `0x4601`.
  Normalise with `parsers.ran.rnti.normalize` (`bare_hex=True` for the UE log).
- **SFN wraps every 1024 frames (~10.24 s)**, so an `SFN.slot` *string* recurs
  across a run. To pair correctly, disambiguate same-`(slot,rnti)` events by
  nearest wall-clock within a tolerance well below one wrap (`correlate_radio.py`
  uses 0.5 s). Never join on the slot string alone over a multi-wrap run.
- **MAC/SCHED lag**: UL events (PRACH/PUSCH/PUCCH) are *processed* several slots
  after the PHY transmission, so a MAC/SCHED line's own slot is later than the
  PHY TX slot. Join MAC/SCHED→PHY via the **`slot_rx=`** field when the build
  emits it (it gives the true PHY reception slot); otherwise join on
  `(SFN.slot, RNTI)` at the PHY layer or calibrate the processing-delay offset.
  (`slot_rx=` is build/verbosity-dependent and absent in the default-config
  example runs.)
- **PRACH is the exception**: the UE prints `sequence_index=` and the gNB prints
  `idx=` — **different numbering**, so do *not* match PRACH on the raw index.
  Correlate PRACH via the resulting **tc-rnti** and the subsequent **Msg3 PUSCH**
  (which has a clean `(SFN.slot, RNTI)` join), or by wall-clock occasion.

## Matching-key priority

1. **PHY `(SFN.slot, RNTI)`** — exact, for PUSCH/PUCCH/PDCCH/PDSCH.
2. **RNTI / TC-RNTI / preamble→tc-rnti chain / TBS / PRB** — to disambiguate UEs
   sharing a slot (RACH contention) and to bridge PRACH→Msg3.
3. **Wall-clock window** — cross-check, and the primary key for non-PHY
   artifacts (pcap NGAP/F1AP/E1AP, 5GC `mme.log`, VIAVI). Valid **only after** the
   source-pair clock offset is known to be ~0 or has been measured (§ Clocks);
   never assume a shared clock for an off-host source.

## Cross-source signals only this layer can see

- **Side attribution**: gNB `PUSCH crc=KO` *with* a matching UE TX at the same
  `(slot,rnti)` ⇒ the UE *did* transmit → gNB-side decode / ZMQ-sample issue,
  **not** a UE DTX. gNB `crc=KO sinr=inf` *without* a UE TX ⇒ true DTX.
  (`correlate_radio.py` labels these `rx-ko/ue-tx` vs `rx-ko/ue-silent`.)
- **PUCCH**: the gNB logs every occasion and resource it checks, the UE only what it sends. A gNB PUCCH shows
  no UE TX when `sinr=-inf` or no field holds valid UCI (`sr=no`, payloads of 2s such as `ack=2`,
  `csi1=2222`). Without a UE TX, one with a HARQ-ACK field is `ue-missed-dl`: a PDSCH was scheduled and the UE
  missed its PDCCH (DL DTX; tracks PDSCH `ue-missing-rx`), or the UE no longer uses the RNTI, e.g. after a
  reestablishment or handover, while the gNB still schedules it: check `ue.log` for the RNTI. Others (SR, CSI) are `no-detection`; a second
  resource of a paired `(slot,rnti)` is `gnb-extra-rx`; neither is a fault. The UE logs RNTI `0000` for the
  Msg4 HARQ-ACK; the script gives it the RNTI of that UE id's last UL TX (the Msg3 TC-RNTI).
- **RACH contention**: two UE PUSCH at the same `(slot, tc-rnti)` from different
  UEIDs, one gNB event → collision (`ue-extra-tx/contention`); expected in
  multi-UE attach, not a fault.
- **Count reconciliation**: UE-side attaches vs gNB `UE created` vs NGAP
  `InitialUEMessage` vs pcap — disagreement localises where UEs are lost. For
  "how many UEs", use `amari-ue`'s cfg-derived count, not the testbed map's
  `amarisoft-ue-N` slot range (slots ≠ simulated UEs; see its `conventions.md`).

## Scripts

| Script | Purpose |
|---|---|
| `resolve.py` | Components, artifacts, testbed map, clock anchors (the `correlate` resolver) |
| `align_clocks.py` | Verify the same-process log↔pcap relationship (sanity/display-TZ guard, not an offset measurement; on split runs the CU log and its `ngap.pcap`, `--cu` to pick one) + UE↔gNB PHY slot alignment (DU log) + `slot_rx=` availability. Does **not** check off-host sources |
| `correlate_radio.py` | Join UE↔gNB PHY events on `(SFN.slot, RNTI)`; flag rx-ko/missing/contention; DTX vs degradation. Needs both a gNB/DU log and a UE log, else exits 1 |
| `map_ue_ids.py` | Per-pcap UE-identifier lifecycle (f1ap/ngap/e1ap); feeds `ue-identity-map.md` |
| `ue_table.py` | One row per UE context, or per UE trace (`--traces`), of a run: F1AP pcap joined with the gNB log and the NGAP/E1AP pcaps (rnti, du_ue, cu_ue, F1AP, NGAP and E1AP ids, lifetime) |

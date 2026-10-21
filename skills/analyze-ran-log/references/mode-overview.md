# Overview mode (playbook)

Produce one consolidated, factual overview: summarize each artifact through its
own type, then add the cross-source layer on top. **Do not** enter the
investigation loop. Ask `AskUserQuestion` only at the end (escalation).

Generic mechanics live here; the per-type specifics (which summary script, which
summary-block template) live in `references/<kind>/overview.md`. Substitute
`${CLAUDE_SKILL_DIR}` inline in commands — the shell doesn't persist env vars
between calls.

## Conduct

- **Factual, not diagnostic.** Report what the artifacts show. A suspicious
  signal is an *anomaly bullet*, not a root cause — escalating is Phase E's job.
- One headline per component. Don't dump per-artifact detail into the overview —
  the value is the consolidated picture.

## Phase A — inventory

Resolve the input if not already done (`SKILL.md` § Resolve & classify). For a
whole run, that is:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/resolve.py <run-dir>
```

Note which components and artifacts are present and their clock anchors. This
drives everything below. A single-artifact input skips straight to Phase B for
its one type.

## Phase B — per-artifact summaries

For **each component present**, read that type's slot and follow it:

```
references/<kind>/overview.md
```

It names the summary script to run and the summary-block template to fill.
Map component → kind with the table in `SKILL.md` § Resolve & classify
(`ocudu-*` → `ocudu`, `amarisoft-ue-*` → `amari-ue`, `*.pcap` → `pcap`,
`*_Command_Log*` → `viavi`).

`amarisoft-mme-*` / `amarisoft-5gc-*` has no type yet — light-touch only: grep
`mme.log` (Retina: `amarisoft-mme-N/mme/<ts>/mme.log`) for
registration / PDU-session / NGAP / `[E]` lines, capped at 200 lines.

Capture one headline per component.

**A missing or unusable artifact is itself an anomaly.** Before summarizing, check
what resolve reported as absent: a component whose primary log is missing, empty,
or lacks its end-of-run marker (closing `(END)` line for OCUDU,
`# Ended on` for the UE sim) is a finding in its own right. Report it, and say
which conclusions it makes unavailable — a truncated log means "not observed", not
"did not happen", and that distinction decides whether a later absence is
evidence.

## Phase C — cross-source alignment

Only when ≥2 components are present. Read `references/correlate/overview.md` and
follow it — it owns the clock/slot alignment step and the list of cross-source
anomalies that no single artifact can reveal.

## Phase D — consolidated overview block

For a **single artifact**, present that type's own summary block from
`references/<kind>/overview.md` and stop.

For a **whole run**, present one consolidated block:

```
## RAN Run Overview

**Path:** <run-dir>
**Components:** <one entry per component resolve reported, with its build/count>
**Clocks:** <from references/correlate/overview.md>

### Per-component
- <component dir>: <one-line headline>
  ...one line per component actually present, named as resolve named it...

### Cross-source picture
- <reconciled counts, radio summary, timeline headline>

### Anomalies
- <bullet per anomaly, or "None">
```

Per-component headlines come from Phase B; the cross-source picture and its
anomalies come from Phase C.

## Phase E — optional escalation

If anomalies were found, end with a single `AskUserQuestion`:

- **Investigate** — read `mode-investigate.md` and enter it on the first anomaly,
  reusing the cached script output from Phase B/C (don't re-run).
- **Query** — read `mode-query.md` and answer a specific question.
- **Done** — no further analysis.

Do not ask if the run was clean — end with the overview.

## Persist learnings

Only if generalisable — route per `references/self-maintenance.md`.

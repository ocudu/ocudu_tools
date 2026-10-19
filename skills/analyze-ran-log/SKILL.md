---
name: analyze-ran-log
description: >
  Analyze, summarize, query, or root-cause RAN test artifacts — a single
  log/config/capture or a whole OCUDU/Retina run directory — and serve as the
  format and procedure reference for each. Covers OCUDU gnb/du/cu/cu_cp/cu_up
  (`gnb.log`, `ocudu_*.yml`, `metrics.json`), Amarisoft UE (`ue.log`,
  `amarisoft_ue.cfg`), VIAVI RU-simulator command logs, NGAP/F1AP/E1AP/MAC-NR/
  RLC-NR pcaps, and cross-artifact correlation (clock/slot alignment,
  UE-identity joining, PRACH/PUSCH sent-vs-received). Triggers:
  "analyze/summarize this run", "why did this test fail", "what went wrong with
  X", "did the UE's PRACH reach the gNB", "correlate the UE and gNB logs", a
  `test_gnb[...]` / `ocudu-*` / `amarisoft-*` directory, a GitLab CI job URL, or
  a format question with no artifact in hand ("what does <log line> mean", "how
  does OCUDU log a handover").
version: 0.2.0
user-invocable: true
allowed-tools: Bash(python3 *analyze-ran-log/scripts*), Bash(python3 -m zipfile *), Bash(LC_ALL=C grep:*), Bash(git log:*), Bash(git show:*), Bash(git diff:*), Bash(git status:*), Bash(git rev-parse:*), Bash(git worktree:*), Bash(curl:*), Bash(glab:*), Bash(ls:*), Bash(grep:*), Bash(find:*), Bash(file:*), Bash(stat:*), Bash(wc:*), Bash(head:*), Bash(tail:*), Bash(sed:*), Bash(sort:*), Bash(uniq:*), Bash(awk:*), Bash(comm:*), Bash(capinfos:*), Bash(tshark:*), Bash(realpath:*), Bash(sha256sum:*), Bash(cat:*), Skill, Edit, Write
---

# Analyze a RAN log / capture / run

Both the **reference** for RAN test artifacts (formats, procedure ladders,
grep/tshark recipes, helper scripts) and the **playbook** for acting on them
(produce an overview, answer a question, root-cause a failure).

Five artifact **types**, each with its own subtree:

| Type | Scope | Subtree |
|---|---|---|
| `pcap` | `*.pcap`/`*.pcapng` — Upper-PDU captures (NGAP/F1AP/E1AP/MAC-NR/RLC-NR), usually five siblings (`mac`,`rlc`,`f1ap`,`e1ap`,`ngap`) per run | `references/pcap/`, `scripts/pcap/` |
| `ocudu` | OCUDU `gnb`/`du`/`cu`/`cu_cp`/`cu_up` artifacts: `gnb.log`, `stdout*.log`, `ocudu_gnb.yml`, `metrics.json` | `references/ocudu/`, `scripts/ocudu/` |
| `amari-ue` | Amarisoft UE simulator: `ue.log`, `stdout.log`, `amarisoft_ue.cfg` | `references/amari-ue/`, `scripts/amari-ue/` |
| `viavi` | VIAVI RU-simulator (TM500) command/event log: `*_Command_Log*.txt` (or its identical `.zip`) | `references/viavi/`, `scripts/viavi/` |
| `correlate` | **Cross-artifact**: line up the same event across UE log, gNB log, and pcaps for a whole run — clock/slot alignment, UE-identity joining, PRACH/PUSCH/PUCCH sent-vs-received | `references/correlate/`, `scripts/correlate/` |

Alongside them, **`references/common/`** holds the artifact-agnostic layer —
protocol/procedure *semantics* (the F1AP message set, the UE-attach ladder), the
UE identifier model, the 3GPP spec map, the FAPI reference. `resolve.py` never
selects it; load it *in addition* to a type subtree when you need the meaning
behind an observation. Axis split: `common/` = *what should happen*, the type
subtrees = *how it looks here*, `correlate/` = *how to line up several
artifacts*. See `references/common/README.md`.

---

## Step 1 — what do you have?

| Input | Do this |
|---|---|
| A **GitLab CI job URL** | Follow `references/ci-retrieval.md` to fetch + unzip into a local dir, then continue as a local run. |
| An **artifact or directory** (`test_gnb[...]`, `ocudu-*`, `amarisoft-*`, a single log/pcap/config) | Resolve it (§ Resolve & classify), then go to Step 2. |
| **No artifact** — a format / semantics / procedure question ("what does this log line mean", "how does OCUDU log a handover", "which pcap carries F1AP") | See § No-artifact questions below. **No resolve, no mode doc** — this is the cheap path. |
| Neither, and it's not a knowledge question | Ask the user for a path or CI URL. |

### No-artifact questions

Split on what the question is really asking:

- **"What *should* happen?"** — a procedure ladder, a message's meaning, an
  identifier definition, a spec clause. Read `references/common/` **first**; that
  is where artifact-agnostic semantics live. Descend into a type only if the
  answer needs the observation surface.
- **"How does it *look* here?"** — the question quotes a concrete log line, field,
  filename, or tool. Pick the type by keyword, then read its
  `references/<type>/reference/`:

  | Keyword in the question | Type |
  |---|---|
  | `gnb.log`, `du.log`, `cu*.log`, `ocudu_*.yml`, `metrics.json`, `ue=N`, `c-rnti=`, a `[LAYER]` tag | `ocudu` |
  | `ue.log`, `amarisoft_ue.cfg`, a 4-hex UE_ID, `5GMM-`, CBR, sim events | `amari-ue` |
  | `.pcap`/`.pcapng`, tshark, a frame number, a dissector field, Upper-PDU | `pcap` |
  | `*_Command_Log*`, `UE Id:`, `I: CMPI`, GETSTATS, `CU PLANE ACTIVE`, TM500 | `viavi` |
  | joining two sources, SFN.slot, clock offset, which-side-failed | `correlate` |

The same vocabulary appears in more than one type (`crc=KO` on the gNB vs
`crc=FAIL` on the UE; four types document a handover). If the question doesn't
disambiguate, answer from `references/common/` and name the per-type difference
rather than guessing which side they meant.

---

## Step 2 — intent gate (artifact present)

Load **at most one** mode playbook, chosen from the user's wording:

| Wording | Load |
|---|---|
| "why", "what went wrong", "root cause", "debug", "investigate", "find the bug" | `references/mode-investigate.md` |
| a concrete answerable question ("how many handovers", "when did the UE attach", "did the UE's PRACH reach the gNB") | `references/mode-query.md` |
| **anything else** — "analyze/summarize this run", "what happened", a bare path or CI URL, no specific question | `references/mode-overview.md` |

**Do not ask the user to pick a mode** — they don't think in modes, and
`mode-overview.md` § Phase E already offers *Investigate / Query / Done* once the
cheap scripted summary is on the table. Explicit wording wins; everything else
defaults to overview.

Modes compose freely: a mode doc may hand off to another by reading it directly
(no re-invocation), and the earlier mode's script output is already in the
session cache — **reuse it, don't recompute**. When the user gives several
instructions, do them in order and ask before switching modes.

---

## Step 3 — read the type subtree

Under `references/<type>/`:

- **`conventions.md`** — type-specific resolve scoping and efficiency deltas.
  **Read this first.**
- **`overview.md`** / **`query.md`** / **`investigate.md`** — the type's slot for
  each activity: the summary-block template, the search-script flags and
  question→command table, and the symptom → `troubleshooting/*.md` dispatch.
  The mode playbook drives; these supply the type-specific part. Load only the
  one the mode needs.
- **`reference/`** — lazily-loaded knowledge: format refs (`log-format.md` /
  `config-format.md` / `pcap-format.md`), protocol field refs (`protocols/`,
  pcap), and per-procedure expected-sequence + vocabulary docs (sequence only;
  failure content lives in the paired `troubleshooting/` doc).
- **`troubleshooting/`** — symptom-first failure playbooks (failure markers +
  investigation checklist). The entry point when starting from a symptom.
- **`scripts/<type>/`** — pre-vetted helpers that emit compact summaries.

`correlate` deviates in two ways: its reference material is flat rather than under
`reference/`, and it carries `procedures/` (cross-artifact traces) instead of
`troubleshooting/`. Its `conventions.md` and three activity slots are the same as
everyone else's.

**Load only what the type needs** — single-artifact work pulls just that type's
subtree, never a sibling's. Two deliberate exceptions: `references/common/` is
pulled by any type when the shared semantics / spec / FAPI are needed; and
`correlate` loads `references/correlate/` *and* draws on the per-artifact
subtrees it joins.

---

## Resolve & classify

One dispatcher classifies the input and runs the matching per-type resolve /
preflight in a single call:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/resolve.py <artifact-or-dir>
```

It classifies the input — a `.pcap`/`.pcapng` or a directory of sibling pcaps →
`pcap`; a `gnb.log` / OCUDU component or run dir → `ocudu`; a `ue.log` /
Amarisoft UE component or run dir → `amari-ue`; a `*_Command_Log*.txt`/`.zip` (or
a dir holding one) → `viavi`; a whole run dir spanning **≥2 RAN application
components** (gNB + UE + 5GC) → `correlate` — then delegates to that kind's
`scripts/<kind>/resolve.py`. It prints the resolved `kind`, the
`-> read references/<type>/` pointer, the per-kind inventory/validation, and a
final `verdict:` line. **Bail if the verdict is not OK.** When the input matches
no kind, it says so — ask the user rather than guessing.

A Retina `test_gnb[...]` directory typically contains:

| Component dir | Artifacts | Type |
|---|---|---|
| `ocudu-gnb-*` / `ocudu-du-*` / `ocudu-cu-*` / `ocudu-cu-cp-*` / `ocudu-cu-up-*` | `gnb.log`/`du.log`/`cu*.log`, `stdout*.log` (`stdout_du.log` of a split DU), `ocudu_*.yml`, `metrics.json` | `ocudu` |
| (the same dirs) | `*.pcap` (`ngap`/`f1ap`/`e1ap`/`mac`/`rlc`) | `pcap` |
| `amarisoft-ue-*` | `ue.log`, `stdout.log`, `amarisoft_ue.cfg` | `amari-ue` |
| VIAVI tester output | `*_Command_Log*.txt`/`.zip` | `viavi` |
| `amarisoft-5gc-*` | `mme.log`, `amarisoft_mme.cfg` | light-touch (future `amari-5gc` type) |
| (whole run, ≥2 components) | cross-artifact correlation | `correlate` |
| (top level) | `testbed.json`, `test.html`, `agent-log-*.log` | the mode playbook |

Per-type scoping notes (multi-component run dirs, sibling-pcap scope, Upper-PDU
dissector preflight) are in `references/<type>/conventions.md`.

---

## Other sources

Beyond this skill's own references:

1. **`spec-explorer`** — when a finding needs the *expected* 3GPP behaviour
   (procedure sequence, IE meaning, timer). Invoke it via the `Skill` tool to get
   canonical, line-numbered spec text rather than reasoning from memory.
2. **OCUDU source** — when behaviour depends on what the implementation actually
   does. `references/ocudu/source-code.md` owns the loading protocol: where to
   find a checkout (`$ANALYZE_RAN_LOG_OCUDU_PATH` → in-project → elsewhere under
   `$HOME` → public repo, with the permission model), matching the run's build,
   and read-only navigation.

---

## Efficiency rules (shared)

These hold for every type; `references/<type>/conventions.md` adds the deltas.

- **Run the helper scripts from within this skill.** The frontmatter
  `allowed-tools` pre-authorizes them only while the skill is active, so they
  execute without permission prompts and no `settings.json` edits are needed.
- **Session cache dir.** All intermediate state (script outputs you may
  post-filter, large spills, staged inputs) lives under one per-session,
  user-private directory:
  `${CLAUDE_CODE_TMPDIR:-/tmp}/claude-skills-${CLAUDE_CODE_SESSION_ID}/`. Write
  files with a **type prefix** — `pcap-`, `ocudu-`, `amari-`, `viavi-`,
  `correlate-` — so the types don't collide. Helper scripts create the dir
  lazily; the OS reaps `/tmp` on reboot, so no manual cleanup.
- **Never** read a raw `gnb.log` / `ue.log` / pcap into context — they can be
  tens to hundreds of thousands of lines (or huge frame dumps). Use the per-type
  summary/search scripts, or grep for specific patterns.
- **Cap** any grep / `tshark -T fields` output at ~200 lines (`| head -n 200`);
  spill larger results to `<cache-dir>/<type>-<purpose>-<sha>.{txt,tsv}` and
  report the path.
- **Prefer** the helper scripts in `scripts/<type>/` over hand-crafted grep /
  tshark chains — they are pre-vetted, cache where useful, and emit compact
  summaries instead of raw lines/frames.
- **Reuse** the cache: if a file you'd produce already exists for the same input,
  post-filter it instead of re-running the script.
- **Clocks/slots**: same-process logs+pcaps (and ZMQ/co-located UE↔gNB) share one
  clock (Δ≈0); off-host sources (VIAVI tester, remote 5GC, a UE sim on another
  box) may be offset — measure per run before comparing wall-clocks, and prefer
  the clock-independent **(SFN.slot, RNTI)** key. Details plus the
  capinfos/tshark display-TZ trap: `references/correlate/cross-correlation.md`.
- In multi-UE runs, scope by UE identifier (`ue=N`, `c-rnti=0xNNNN`, UE-id,
  `--rnti`) **early** — the cross-product over UEs is large.

---

## Memory & self-maintenance

When — and **only** when — a session surfaces something to persist (a
generalisable learning, or a doc/script that is wrong/stale), or the user asks to
"reorganize <type> knowledge" / "reorganize the mode playbooks", **load
`references/self-maintenance.md` and follow it**. It covers what is/isn't worth
saving, where each kind goes, the one-way layering, and the confirm-before-edit
flow.

Guardrails (always): edit only this skill's own trees, never elsewhere, and never
`git add`/`commit`/`push` — leave diffs for the user to review.

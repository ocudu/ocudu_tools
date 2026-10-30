---
name: ocudu-code-review
description: >
  Review OCUDU changes for correctness, OCUDU C++ conventions, real-time-path
  safety and security. Use when asked to review OCUDU code, a diff, a branch
  or a GitLab MR. Trigger phrases: "review this OCUDU change", "code review
  this branch/diff/MR", "check this for RT-path safety", "security review this
  OCUDU code", "/ocudu-code-review".
argument-hint: "[target] [--base=<ref>] [--fanout]"
metadata:
  version: 0.1.0
license: BSD-3-Clause-Open-MPI
compatibility: >
  Requires git, plus network access to `origin` for the base fetch. A GitLab
  MR URL resolves through `origin` (`refs/merge-requests/<iid>/head`), so
  `origin` must point at the same gitlab.com/ocudu project as the MR. An
  authenticated `glab` is optional: with it, an MR is compared against its
  real target branch and its existing review threads are digested so the
  review doesn't repeat them; without it, against `origin/dev` and blind to
  the threads.
allowed-tools: Bash(*ocudu-code-review/scripts/resolve_diff.sh*), Bash(git diff:*), Bash(git log:*), Bash(git show:*), Bash(git rev-parse:*), Bash(git branch:*), Read, Grep, Glob, Agent, ReportFindings
---

# OCUDU Code Review

**Usage:** `/ocudu-code-review [target] [--base=<ref>] [--fanout]`

Defaults: `target` = current branch, `--base` = the MR's target branch else
`origin/dev`. Anything in the argument string that isn't a flag is the
`target`. `--fanout` reviews a large diff through subagents.

## Guidelines

- Find problems; do not fix them. No findings is a valid outcome.
- Do not build or test locally without asking the user first; rely on CI.
- Do not launch other code review skills, or subagents beyond what the PLAN
  says, without asking the user first.

## Step 1 — Resolve and plan

```
"${CLAUDE_SKILL_DIR}/scripts/resolve_diff.sh" "$ARGUMENTS"
```

It prints `KEY=value` lines and a `PLAN:` block. Follow the PLAN, then Step 2.
`--help` lists accepted targets and keys.

Never trim diff context below the default three lines.

The PLAN's diff command prefixes each added or context line with its line
number in the new file. Every `file:line` you report is that number, never a
position in the diff or in a saved tool-output file. For a removed line, cite
the nearest numbered line.

## Step 2 — Review

Check every hunk against every checklist in:

- [REFERENCE.md](references/REFERENCE.md): correctness, C++ conventions,
  comment style.
- [realtime.md](references/realtime.md): real-time path safety.
- [security.md](references/security.md): untrusted input.

Skip stylistic nits that match no checklist item.

## Step 3 — Report findings

Merge and dedupe any subagent findings first. Each finding has:

- `category` — kebab slug: `correctness`, `memory-safety` (lifetime/UB bugs
  not caused by untrusted input), `untrusted-input`, `realtime-alloc`,
  `realtime-blocking`, `realtime-latency`.
- `failure_scenario` — the concrete inputs/state that lead to the wrong
  outcome.
- `verdict` — `CONFIRMED` when traced end to end, else `PLAUSIBLE`. Drop
  anything weaker.

Rank findings most-severe-first: memory-safety, untrusted-input and real-time,
then correctness. Output, in this order:

1. **Summary**: 1–3 lines on what the change does.
2. **Findings**: one `ReportFindings` call with the full list (empty if none).
   If the tool is unavailable, a markdown table with columns
   `File:Line | Category | Verdict | Summary | Failure scenario`.
3. **`### Conventions`**: convention and comment-style findings, one
   `file:line — issue` per line. Omit if none.
4. **Reviewed**: files and diff range, one line.

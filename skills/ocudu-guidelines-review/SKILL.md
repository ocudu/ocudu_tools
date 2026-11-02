---
name: ocudu-guidelines-review
description: >
  Review OCUDU code against the OCUDU code guide, read from the guide itself, and fix the
  violations in the checkout. Writes a report plus a patch. Use for "guidelines review",
  "apply the code guide", "check this branch/folder against the code guide",
  "/ocudu-guidelines-review".
argument-hint: "[diff [BASE] | path <dir>... | all]"
user-invocable: true
metadata:
  version: 0.1.0
license: BSD-3-Clause-Open-MPI
compatibility: >
  Run from an OCUDU checkout. Needs git, cmake, jq, include-what-you-use, clang-format-21 and
  clang-format-diff-21, and the code guide from an ocudu_docs checkout (next to the OCUDU
  checkout, or wherever OCUDU_CODE_GUIDE points). The build tree defaults to <repo>/build
  (OCUDU_BUILD_DIR). Fixes are made in the checkout's working tree; results go to
  $XDG_CACHE_HOME/ocudu-review, else ~/.cache/ocudu-review (REVIEW_RESULTS_DIR).
---

# OCUDU code-guide review

**Usage:** `/ocudu-guidelines-review [diff [BASE] | path <dir>... | all]` — default `diff`.

The SRS code guide is the rulebook; this skill holds no rules of its own. It checks every rule of the guide over every
scoped file and fixes every violation it finds. Every step that needs no judgement is a script in this skill's
`scripts/`; run them as shown and do not redo their work by hand.

## 0. Set up

```sh
d=$(bash "${CLAUDE_SKILL_DIR}/scripts/setup.sh" $ARGUMENTS)
```

It finds the checkout (the current repo, or `OCUDU_REPO`), its build tree and the code guide, writes `scope.txt` and
`review.env` into a new results directory, records the tree as the review's baseline, and prints the directory: that is
`$d` in every later step. Its notes (scope size, uncommitted changes) go to stderr. If it fails (no C/C++ files in
scope, bad base, code guide not found), stop and tell the user its error.

- **Diff scope:** apply rules to the entities (declarations, functions, classes, statements) the diff added or changed.
  Leave untouched code alone, even when it breaks a rule.
- **Path / all scope:** apply rules to whole files.
- **Fixes that reach outside the scope:** when a fix changes an interface, also update every user of it: call sites, the
  `.cpp` that implements a header, files that relied on an include through a header you removed it from, and tests. Find
  them with `grep -rn` over `lib include apps tests`, and list each out-of-scope file you edit in the report.
- **Delegating to subagents:** instructions given to subagents may add *how* to apply a rule (naming, order, splitting)
  but may never narrow *which* code a rule applies to. Pass the guide sections verbatim; do not summarise rules.

## 1. Tool checks

```sh
python3 "${CLAUDE_SKILL_DIR}/scripts/tool_checks.py" "$d"
```

It writes `$d/tool-checks/`.

- **Hex literals.** Lowercased in place in the scoped files, with the expression of CI's `hex2lowercase.sh`. The lines
  it changed are in `hex.txt`.
- **Include hygiene.** `include-what-you-use` on each scoped source file, and on the source file that implements each
  scoped header with `--check_also`. It configures the build tree if it has no `compile_commands.json`. It only
  reports: the raw output of each run is in `iwyu/<tu>.log`, and `summary.md` lists per scoped file how many lines
  iwyu would add or remove. Read the log of every file marked `changes` and apply it through the guide's include rules,
  not as the verdict: transitive includes are fine, so do not add every include iwyu recommends, only the needed ones;
  what matters most is removing unused includes and replacing includes with forward declarations where possible.

`summary.md` starts with the two table rows for the report. Copy them as they are, filling in the iwyu Fixed /
Reported counts once you have applied its suggestions. A tool that is missing or fails is already recorded there as the
check's result (e.g. a file not in `compile_commands.json`); report it and go on, do not work around it by hand.

## 2. Code guide

The rulebook is the directory `GUIDE` in `$d/review.env`. Read its `index.md`, then every numbered `.md` file, in
full, before touching code. Every rule in them applies to every scoped file. Fix every violation in the checkout; §3
lists the only exceptions.
When a fix needs a choice (a name, how to split parameters, a doc sentence), follow the conventions of the surrounding
module and record the choice in the report.

## 3. Report-only cases

Fix everything except:
- Commit-message rules: commits are not rewritten here. Put the proposed message in the report (diff scope only).
- A fix that, after reasonable attempts, does not compile or breaks a test. Revert it and report it with the error.
- A fix that would need a change in a generated file, a bundled or external library, or another repository.

This list is closed. Do not invent other exemptions: not by origin of the code ("generated types", "ASN.1",
"converters"), not by volume ("too many occurrences", "wholesale rewrite"), not by risk. When a rule seems not to apply,
say in the report which property of the code makes it impossible (e.g. "`*_c` choice type: not an aggregate"). "n/a"
without such a reason counts as a skipped check. If a fix is too large for one patch, do it anyway and propose in the
report how to split it into MRs.

## 4. Validate

After all fixes:

```sh
bash "${CLAUDE_SKILL_DIR}/scripts/validate.sh" "$d"
```

It finds the edited files itself (the tree against the baseline, out-of-scope files included), formats them (diff
scope: only the changed lines), builds the CMake targets of every edited source and of every source that includes an
edited header, and runs the tests of the test executables among them. Formatting runs after all fixes so it also covers
the code the fixes added. Logs go to `$d/validation/`; `summary.md` there holds the report's Validation row.

If it fails, read the log it names, fix or revert the fix that broke it (§3), and run it again until it passes.

## 5. Output

Write `$d/report.md` with this layout, then the patch:

```markdown
# Guidelines review — <SCOPE>
Repo: <REPO> at <COMMIT> (<BRANCH>)   Base: <BASE>, merge-base <MERGE_BASE> (diff scope only)
Files: <n> (scope.txt), plus <m> out-of-scope files edited by fixes

| Check | Result | Fixed | Reported |
| ----- | ------ | ----- | -------- |
| hex literals | ok / changes / failed: <why> | <n> | <n> |
| iwyu | ... | | |
| <guide file> § <section heading> | clean / changes / n/a: <why> | <n> | <n> |
| ... one row per section heading of every guide file ... | | | |
| Validation (build + tests) | ok / failed: <what> | – | – |

## Findings
<guide file> § <section> — `path:line` — <what was wrong> — fixed in patch | report only: <why>
  Choice: <the decision made, when the fix needed one>

## Out-of-scope files edited
<path> — <guide section that required it>
```

The `<UPPERCASE>` fields are the values in `$d/review.env`.

```sh
bash "${CLAUDE_SKILL_DIR}/scripts/patch.sh" "$d"
```

It writes `$d/review.patch` (every change since the baseline, nothing else) and prints the paths. A guide section with
no row in the table counts as a skipped check. End by telling the user the paths it printed, that the fixes are also
in the checkout's working tree, and how many findings remain unfixed.

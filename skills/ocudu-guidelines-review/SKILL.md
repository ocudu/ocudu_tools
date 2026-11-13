---
name: ocudu-guidelines-review
description: >
  Review OCUDU code against the OCUDU code guide, read from the guide itself, and fix the
  violations in the checkout. Writes a report plus a patch. Use for "guidelines review",
  "apply the code guide", "check this branch/folder against the code guide",
  "/ocudu-guidelines-review".
argument-hint: "[--exhaustive] [diff [BASE] | path <dir>... | all]"
user-invocable: true
metadata:
  version: 0.2.0
license: BSD-3-Clause-Open-MPI
compatibility: >
  Run from an OCUDU checkout. Needs git, cmake, jq, include-what-you-use, clang-format-21 and
  clang-format-diff-21, and the code guide from an ocudu_docs checkout (next to the OCUDU
  checkout, or wherever OCUDU_CODE_GUIDE points). The build tree defaults to <repo>/build
  (OCUDU_BUILD_DIR). Fixes are made in the checkout's working tree; results go to
  $XDG_CACHE_HOME/ocudu-review, else ~/.cache/ocudu-review (REVIEW_RESULTS_DIR).
---

# OCUDU code-guide review

**Usage:** `/ocudu-guidelines-review [--exhaustive] [diff [BASE] | path <dir>... | all]` — default `diff`.

`--exhaustive` splits the review into small work items (a few guide sections × a few files) done by subagents, and
scripts check that every (guide section, file) pair was reviewed and every finding was dealt with (§2E). It is slower
and costs more; use it when nothing may be missed.

The SRS code guide is the rulebook; this skill holds no rules of its own. It checks every rule of the guide over every
scoped file and fixes every violation it finds. Every step that needs no judgement is a script in this skill's
`scripts/`; run them as shown and do not redo their work by hand.

## 0. Set up

```sh
d=$(bash "${CLAUDE_SKILL_DIR}/scripts/setup.sh" $ARGUMENTS)
```

It finds the checkout (the current repo), its build tree and the code guide, writes `scope.txt` and
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

If `EXHAUSTIVE=1` in `$d/review.env`, do §2E instead of this section.

The rulebook is the directory `GUIDE` in `$d/review.env`. Read its `index.md`, then every numbered `.md` file, in
full, before touching code. Every rule in them applies to every scoped file. Fix every violation in the checkout; §3
lists the only exceptions.
When a fix needs a choice (a name, how to split parameters, a doc sentence), follow the conventions of the surrounding
module and record the choice in the report.

## 2E. Code guide, exhaustive mode

Only when `EXHAUSTIVE=1` in `$d/review.env`. Subagents review and fix the scoped files; you plan, dispatch, check and
merge. Do not review or fix the scoped files yourself, and do not read the ledgers: the scripts check them. §3, §4 and
§5 still apply.

The subagent prompts below are exact. Do not add to them, summarise rules in them, or narrow them: each brief already
holds the rules verbatim and the instructions. Start subagents in parallel, several per message; each replies with one
line.

1. **Your own part.** Apply the iwyu suggestions (§1); read the guide's include sections for that. Guide files whose name
   contains "commit" are yours too (diff scope only, as in §3).
2. **Plan.**
   ```sh
   python3 "${CLAUDE_SKILL_DIR}/scripts/exhaustive.py" plan "$d"
   ```
   It writes one brief per work item and lists the items in `$d/exhaustive/items.txt`. If it fails because there are
   too many items, stop and tell the user its message.
3. **Review.** For each item in `items.txt`, start a general-purpose subagent with the prompt:
   `Read <d>/exhaustive/review/<item>.md and do exactly what it says. Reply only with the line it asks for.`
4. **Check the review.**
   ```sh
   python3 "${CLAUDE_SKILL_DIR}/scripts/exhaustive.py" review "$d"
   ```
   For each item it names as failing, start a new subagent with the same prompt, then run it again; repeat until it
   passes. An item that fails three times you do yourself, following its brief. Once it passes it numbers the findings
   and writes one fixer brief per batch with findings, listed in `$d/exhaustive/fix-batches.txt`.
5. **Fix.** For each batch in `fix-batches.txt`, start a general-purpose subagent with the prompt:
   `Read <d>/exhaustive/fix/<batch>.md and do exactly what it says. Reply only with the line it asks for.`
   Batches hold disjoint files, so fixers run in parallel safely. Start them without worktree isolation: they must
   edit this checkout, since `review.patch` is the diff of its working tree.
6. **Check the fixes.**
   ```sh
   python3 "${CLAUDE_SKILL_DIR}/scripts/exhaustive.py" fixes "$d"
   ```
   Re-run failing batches as in step 4. Once it passes it writes `report-rows.md`, `findings.md` and
   `outside-edits.md` in `$d/exhaustive/`.
7. **Edits outside the batches.** Make every edit in `outside-edits.md` yourself (§0: fixes that reach outside the
   scope).
7b. **Recheck the fixes.** Fixers write code that no reviewer read, and a fix can break a rule other than the one it
   fixes. Review that code:
   ```sh
   python3 "/home/dev/.claude/skills/ocudu-guidelines-review/scripts/exhaustive.py" recheck "$d"
   ```
   It plans a diff-scope review, in `$d/recheck`, of every C/C++ line changed since the baseline (the fixers' and your
   own edits). Do steps 3 to 7 on `$d/recheck` instead of `$d`, with the same subagent prompts (their paths start with
   `<d>/recheck/`). If it finds nothing, go on. Otherwise its findings are fixed in the checkout and recheck again
   (`rm -r "$d/recheck"` first), at most twice; a finding left after that goes in the report as report-only, with why.
   Its `findings.md` and `outside-edits.md` are part of the report's Findings and out-of-scope files.
8. **Validate** (§4). When you revert a fix because it breaks the build or a test, set its status in
   `$d/exhaustive/fix/<batch>.json` to `report-only` with the error as its `note`, and run step 6 again so the report
   files match the patch.
9. **Report** (§5). The guide rows of the table are `report-rows.md` as it is, filling in the rows it leaves to you; the
   Findings are `findings.md` as it is, plus your own; the out-of-scope files are those of `outside-edits.md` and
   your own. A `rejected` finding is one a fixer judged not to break the rule; it stays in the report, with why.

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
scope: only the changed lines), searches the added lines for violations a regular expression finds (`const` on a
local of value type, `\param` without a direction; it fails when it finds one), builds the CMake targets of every edited source and of every source that includes an
edited header, directly or not, and runs the tests of the test executables among them and of those linking them.
Formatting runs after all fixes so it also covers the code the fixes added. Logs go to `$d/validation/`; `summary.md` there holds the report's Validation row.

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

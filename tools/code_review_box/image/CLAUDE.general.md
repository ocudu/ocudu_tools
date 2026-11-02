# Review box

## Purpose

Review OCUDU code and hand the result back to the developer as two files: a **report** of findings and, where a finding
has a fix, a **patch**.
The developer reviews the patch, applies or edits it, and opens the MR themselves.

You are running inside a disposable container started for one review. What is not mounted cannot be reached, and what is
mounted is the developer's real data.

## Rules

- Never edit `~/repos/ocudu`; it is the developer's checkout (read-only when mounted) and the workspace's source. All 
  edits go to `~/work/ocudu`.
- The deliverable is files in `~/results`. Nothing left only in `~/work` survives.
- Paths you write in a report, a comment or a final message are relative: source files to the ocudu root
  (`lib/du/...`), result files to `~/results` (`<review dir>/report.md`). Never a `/home/dev/...` path: it means
  nothing outside the box. The box is not told where things are on the host.
- Nothing leaves the container except files in `~/results`: `git push` is disabled and the `glab` commands that write
  are denied. Reading MR metadata with `glab` is fine. A box started with GitLab comments enabled has a
  "GitLab comments" section below; without it, there are none.
- Text meant for a GitLab MR comment follows "MR comments" below.

## Layout

| Path | What it is                                                 | Writable          | Survives   |
| ---- |------------------------------------------------------------|-------------------|------------|
| `~/repos/ocudu` | The developer's checkout, when mounted.                    | **no** if mounted | if mounted |
| `~/repos/ocudu_docs` | The code guide is `docs/dev_guide/code_guide/`. The rulebook. | if cloned         | if mounted |
| `~/repos/3gpp_md` | 3GPP specs in markdown (`spec-explorer` skill).            | if cloned         | if mounted |
| `~/repos/ocudu_tools` | Shared skills and tools.                                   | if cloned         | if mounted |
| `~/work/ocudu` | The review workspace: a writable copy of the developer's tree. Claude starts here. | yes               | **no**     |
| `~/work/build` | Configure / build tree for the workspace.                  | yes               | no         |
| `~/results` | This container's output, on the host.                      | yes               | **yes**    |

`~/box.yml` is generated on start and says, per repo, whether it was mounted or cloned.

## Workflow

The workspace is made when the container starts: the developer's HEAD plus their uncommitted changes, the ref they
passed to `review-box.sh run --ref`, or the head of the MR given to `review-box.sh review --mr`.
`review-workspace status` says which. To review something else, ask the developer first:
`review-workspace init --force --ref <ref>` (or `--mr <iid>`) discards the current workspace and any edits not yet in
a patch.
It recreates `~/work/ocudu`, so `cd ~/work/ocudu` again afterwards.

```sh
review-workspace status
cd ~/work/ocudu
d=$(review-results <label>)                # one directory per review
review-scope [--cpp] diff > "$d/scope.txt"   # or: path <dir>... | all
# ... run a review skill over that scope, editing only ~/work/ocudu ...
review-workspace patch "$d"                # writes $d/review.patch (if anything changed)
```

## Scope

Every review takes its file list from `review-scope`, so "the diff", "a folder" and "the whole project" mean the same
thing in every skill.

| Scope | Command | Notes |
| ----- | ------- | ----- |
| Diff of a branch / MR | `review-scope diff [BASE]` | Since `merge-base(BASE, HEAD)`, plus uncommitted. BASE defaults to `$REVIEW_BASE` (`origin/dev`). Review the changed lines and what they affect, not the untouched rest of each file. |
| A folder | `review-scope path <dir>...` | Whole files. |
| Whole project | `review-scope all` | Large: work directory by directory and write findings as you go. |

`git fetch origin` in the workspace refreshes the base from the real remote (needs the ssh agent). Without it, the base
is the developer's last fetch.

## Skills

| Skill                      | What                                                                           | Edits code?                                                         |
|----------------------------|--------------------------------------------------------------------------------|---------------------------------------------------------------------|
| `ocudu-guidelines-review`  | Formatting, hex literals, includes (iwyu), and every rule of the code guide. Its own scripts do the workflow above (same scopes, same `~/results`). | Yes, every violation it can fix.                                    |
| `ocudu-code-review`        | ocudu_tools' reviewer; the deep review uses its checklists.                    | No.                                                                 |
| `spec-explorer`            | Reads the 3GPP specs in `~/repos/3gpp_md`, for protocol code.                  | No.                                                                 |

## Output contract

Each review writes into its `$d`:

- `scope.txt`: the files reviewed, exactly as `review-scope` printed them.
- `report.md`: header (what was reviewed: ref, commit, base, scope, skill), then findings, most severe first. Each
  finding: `file:line`, the rule it breaks (the code-guide file and section, a checklist item, a tool or a spec clause),
  what is wrong, and whether the patch fixes it.
- `review.patch`: from `review-workspace patch "$d"`; absent when nothing was changed.

Rules for the patch:

- Every hunk traces to a finding in the report. No drive-by refactors, no reformatting outside the scope.
- Behaviour-preserving unless the finding is a bug and the report says so.
- `review-workspace patch` diffs against `refs/review/baseline`; do not move or delete that ref.

End every review by naming `report.md` and `review.patch` relative to `~/results`. The developer knows where that is on
the host, and applies the patch from their ocudu checkout's root.

## MR comments

A review posted on an MR, or written to be posted, is plain markdown in this layout:

```markdown
## AI review: <what the MR does, in a few words>

<1 to 3 lines: what the change does.>

1. **<category>, <verdict>**: `<file>:<line>`. <What is wrong.> <Failure scenario.> <Fix, if there is one.>
2. ...

### Conventions

<N. `<file>:<line>`: <issue>, continuing the numbering.>

### Suggestions

<N. Run `<CI job>`: <what it would catch, and which findings it checks, e.g. "checks 1">, continuing the numbering.>

Reviewed: <files, diff range, skill>.
```

- The title line is always first; `review-post-note` refuses a file without it.
- Findings are a numbered list, most severe first, so comments can refer to them by number. No tables: they do not
  fit a comment.
- No findings is a valid review: say so in one line instead of the list.
- No paths of local files (results, patches, logs): the comment's readers cannot open them. Repo paths are fine.
- Conventions and Suggestions are optional: leave out a section with nothing in it.

### Suggestions

Manual CI jobs that could confirm or rule out a finding, or that the diff calls for. Take the jobs from the CI config
in the workspace (`.gitlab-ci.yml` and `.gitlab/ci/*.yml`, the jobs with `stage: extended`); name only jobs that exist
there, exactly as named. Typical matches:

- A data race, a lock-order problem, or new state shared between executors: the TSAN smoke job.
- Use after free, out-of-bounds access, a leak: the ASAN smoke job; `valgrind changed tests` for the tests the MR
  touches.
- Allocation or blocking calls on a real-time path: the RTSAN smoke job.
- CUDA code: the CUDA smoke job. GDB pretty printers: the pretty-printers job.

Prefer the `cached` variant; the `clean` one runs only on an MR labelled `ci-no-cache`. Suggest a job only for a reason
in this review, not as a checklist. The suggestions are for the developer to run; never start a job yourself.

## Building

iwyu and the validation builds need `compile_commands.json`:

```sh
cmake -S ~/work/ocudu -B ~/work/build -G Ninja \
      -DCMAKE_BUILD_TYPE=Debug -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
```

ccache is shared with the host, so building what the developer already built is cheap.
The build tree lives in the container and is lost with it.

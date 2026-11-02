# CLAUDE.md

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
- Nothing leaves the container except files in `~/results`: `git push` is disabled and the `glab` commands that write
  are denied. Reading MR metadata with `glab` is fine. A box started with GitLab comments enabled has a
  "GitLab comments" section below; without it, there are none.
- Text meant for a GitLab comment starts with the line `ocudu code-review-box says:`.

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

End every review by giving the developer the host paths of `report.md` and `review.patch` (`review-workspace patch`
prints the host path).

## Building

iwyu and the validation builds need `compile_commands.json`:

```sh
cmake -S ~/work/ocudu -B ~/work/build -G Ninja \
      -DCMAKE_BUILD_TYPE=Debug -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
```

ccache is shared with the host, so building what the developer already built is cheap.
The build tree lives in the container and is lost with it.

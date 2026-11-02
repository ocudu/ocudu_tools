# code_review_box

A container for reviewing OCUDU code with Claude Code, locally ([ocudu#874](https://gitlab.com/ocudu/ocudu/-/work_items/874)). It carries what a review
needs: the OCUDU checkout, the SRS code guide, the 3GPP specs, the build toolchain, clang-format, include-what-you-use,
and review skills.
The output is a report and a `git diff` patch that the developer reviews, applies or edits, and turns into an MR
themselves. Running it in CI is a separate issue.

## Quick start

```sh
cp .env.example .env                                 # set SRC_OCUDU, SRC_OCUDU_DOCS, SRC_OCUDU_TOOLS
./review-box.sh run my-branch                        # builds the image the first time, opens a shell
./review-box.sh run --ref origin/feature-x mr-1234   # or review a given branch/commit

# inside the container: the workspace is already made, so you can just start a claude session and use the skills:
claude
> /ocudu-review-mechanical diff
> /ocudu-review-expert path lib/scheduler
> /ocudu-review-concurrency path lib/scheduler
> /ocudu-review-testing path lib/scheduler

# back on the host, you can apply the resulting patch to review it:
git -C ~/repos/ocudu apply results/my-branch/<set>/review.patch
```

The name is usually the branch: `run feature/foo` works, and the box is called `feature-foo` (Docker allows no `/` in
container names). `rm` accepts either spelling. Other commands: `review-box.sh ls`, `rm <name>`, `prune`, `build`.

## Layout

| Path | Contents                                                                                                                                                                                                                                                                                                                                              |
| ---- |-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `review-box.sh` | Host launcher: build, run, list, remove.                                                                                                                                                                                                                                                                                                              |
| `docker-compose.yml` | Mounts and environment.                                                                                                                                                                                                                                                                                                                               |
| `.env.example` | Template for `.env` (gitignored): repo paths, base branch.                                                                                                                                                                                                                                                                                            |
| `image/Dockerfile` | The single image: toolchain, OCUDU build deps, clang-format/tidy, iwyu, reuse, Claude Code.                                                                                                                                                                                                                                                           |
| `image/entrypoint.sh` | On start: copies Claude/git config from the host, mounts or clones repos, links skills.                                                                                                                                                                                                                                                               |
| `image/bin/review-workspace` | Creates the writable review copy and writes the patch.                                                                                                                                                                                                                                                                                                |
| `image/bin/review-scope` | The file list for a review: `diff`, `path`, or `all`.                                                                                                                                                                                                                                                                                                 |
| `image/bin/review-results` | A new output directory under `~/results`.                                                                                                                                                                                                                                                                                                             |
| `image/bin/git-remote-push-disabled` | Refuses every git push from the box (see Permissions).                                                                                                                                                                                                                                                                                                |
| `image/skills.txt` | Which skills are linked in.                                                                                                                                                                                                                                                                                                                           |
| `image/CLAUDE.md` | Installed as `~/.claude/CLAUDE.md`: the one place for paths, rules, workflow, scope and output contract. Claude starts in `~/work/ocudu`, so ocudu's own project instructions load too. |
| `image/settings.json` | Installed as `~/.claude/settings.json`: denies Claude the commands that push or post to GitLab (see Permissions). |
| `skills/` | `ocudu-review-mechanical` lives in ocudu_tools' `skills/` and is linked in from there (`image/skills.txt`); planned: `ocudu-review-expert` (search for correctness, 3GPP issues, life cycle issues, design smells and performance issues), `ocudu-review-concurrency` (search for concurrency issues), `ocudu-review-testing` (search for testing completion and simplicity). |
| `results/` | Output, one directory per container. Gitignored.                                                                                                                                                                                                                                                                                                      |

## How it works

- **Your checkout is never written to.** `ocudu` (and every other mounted repo) is mounted read-only.
  On start, the box builds `~/work/ocudu`, a repo that borrows the objects of your checkout (git alternates, so instant)
  and reproduces your HEAD plus your uncommitted and untracked files (or the `--ref` given to `run`).
  Skills edit that copy; `review-workspace patch` diffs it against the recorded baseline, so `review.patch` contains
  only the review's changes.
- **Scope is one script.** `review-scope diff|path|all` decides which files a review covers, so every skill agrees on
  what "the diff" or "this folder" means.
- **Mounted or cloned.** Each repo is bind-mounted when `SRC_<REPO>` is set in `.env`, or cloned into the container on
  start otherwise (`CLONE_<REPO>`, `REF_<REPO>`).
- **Results persist; everything else does not.** `~/results` is `results/<name>/` on the host. The workspace and build
  tree die with the container; ccache is shared with the host, so rebuilding is cheap.

## Permissions

`claude` inside the container runs in its normal permission mode; whether to relax that is a decision for the team. The
read-only mounts are what keep the developer's checkout safe either way.

The box never pushes or posts to GitLab:

- **git push is disabled.** The entrypoint rewrites every push to a network remote (`url.push-disabled://.pushInsteadOf`)
  to `image/bin/git-remote-push-disabled`, which refuses it. Fetch and clone still work, and `--no-verify` does not
  skip it. It does not cover a remote with an explicit `pushurl`.
- **Claude is denied the write commands.** `image/settings.json` (installed as `~/.claude/settings.json`) denies `git push`, changing the push
  rewrite, the `glab mr` / `glab issue` subcommands that write, and `glab api` with a method or body. Reading MR
  metadata (`glab mr view`, `glab mr diff`, `glab api <GET endpoint>`) stays allowed. These rules apply to the commands
  Claude runs, not to commands scripts run for it.

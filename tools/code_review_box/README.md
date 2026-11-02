# code_review_box

A container for reviewing OCUDU code with Claude Code, locally ([ocudu#874](https://gitlab.com/ocudu/ocudu/-/work_items/874)). It carries what a review
needs: the OCUDU checkout, the SRS code guide, the 3GPP specs, the build toolchain, clang-format, include-what-you-use,
and review skills.
The output is a report and a `git diff` patch that the developer reviews, applies or edits, and turns into an MR
themselves. Running it in CI is a separate issue.

## Quick start

```sh
cp .env.example .env                                 # set SRC_OCUDU, SRC_OCUDU_DOCS
install -Dm600 /dev/null ~/.config/review-box/secrets.env   # then add tokens there, never in .env:
                                                     # CLAUDE_CODE_OAUTH_TOKEN (claude setup-token), GITLAB_TOKEN
./review-box.sh run my-branch                        # builds the image the first time, opens a shell
./review-box.sh run --ref origin/feature-x mr-1234   # or review a given branch/commit

# inside the container: the workspace is already made, so you can just start a claude session and use the skills:
claude
> /ocudu-guidelines-review diff
> /ocudu-review-expert path lib/scheduler
> /ocudu-review-concurrency path lib/scheduler
> /ocudu-review-testing path lib/scheduler

# back on the host, apply the patch from the root of your ocudu checkout:
cd ~/repos/ocudu && git apply <this dir>/results/my-branch/<set>/review.patch
```

Unattended MR review: the box checks out the MR head, runs `claude -p` with a prompt and an allow-list, and prints the
report. `--post` posts it on the MR from the host; the box itself never writes to GitLab.

```sh
./review-box.sh review --mr 1234                     # or the MR URL; other projects: REVIEW_MR_PROJECT
./review-box.sh review --mr 1234 --post              # also post the report as an MR note
./review-box.sh review --mr 1234 --prompt my.md      # another prompt; {MR_URL}, {BASE}, {IID}, {PROJECT}, {PREVIOUS_REPORT} are filled in
./review-box.sh review --mr 1234 --glab-comment --prompt inline.md   # Claude may post MR notes itself
```

The report is also kept in `results/<project>-mr-<iid>/` (e.g. `results/ocudu-ocudu-mr-1234/`); the next review of the
same MR reads it and rechecks its findings. A review comment starts with `## AI review: <description>` and
lists its findings as a numbered list. Tools outside `image/headless-settings.json` are refused, not asked for.

The name is usually the branch: `run feature/foo` works, and the box is called `feature-foo` (Docker allows no `/` in
container names); its container is `feature-foo.<user>`, so users sharing a Docker daemon can reuse names. `rm`
accepts either spelling. Other commands: `review-box.sh ls`, `rm <name>`, `prune` (your
containers and volumes; `--image` also removes the image all users share), `build`.

## Layout

| Path | Contents                                                                                                                                                                                                                                                                                                                                              |
| ---- |-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `DESIGN.md` | Design choices. |
| `review-box.sh` | Host launcher: build, run, list, remove.                                                                                                                                                                                                                                                                                                              |
| `docker-compose.yml` | Mounts and environment.                                                                                                                                                                                                                                                                                                                               |
| `.env.example` | Template for `.env` (gitignored): repo paths, base branch.                                                                                                                                                                                                                                                                                            |
| `image/Dockerfile` | The single image: toolchain, OCUDU build deps, clang-format/tidy, iwyu, reuse, Claude Code.                                                                                                                                                                                                                                                           |
| `image/entrypoint.sh` | On start: writes the Claude config, copies git config from the host, mounts or clones repos, links skills.                                                                                                                                                                                                                                                               |
| `image/bin/review-workspace` | Creates the writable review copy and writes the patch.                                                                                                                                                                                                                                                                                                |
| `image/bin/review-scope` | The file list for a review: `diff`, `path`, or `all`.                                                                                                                                                                                                                                                                                                 |
| `image/bin/review-results` | A new output directory under `~/results`.                                                                                                                                                                                                                                                                                                             |
| `image/bin/git-remote-push-disabled` | Refuses every git push from the box (see Permissions).                                                                                                                                                                                                                                                                                                |
| `image/CLAUDE.glab-comment.md` | Appended to `~/.claude/CLAUDE.md` when GitLab comments are enabled. |
| `image/bin/review-post-note` | The only way Claude posts an MR note: checks the prefix (GitLab comments only). |
| `image/headless-settings.json` | Allow-list of the tools an unattended review may use. |
| `prompts/` | Prompts of unattended reviews; `mr-review.md` is the default. |
| `image/skills.txt` | Which skills are linked in.                                                                                                                                                                                                                                                                                                                           |
| `image/CLAUDE.general.md` | Installed as `~/.claude/CLAUDE.md`: the one place for paths, rules, workflow, scope and output contract. Claude starts in `~/work/ocudu`, so ocudu's own project instructions load too. |
| `image/settings.json` | Installed as `~/.claude/settings.json`: denies Claude the commands that push or post to GitLab (see Permissions). |
| `skills/` | `ocudu-guidelines-review` lives in ocudu_tools' `skills/` and is linked in from there (`image/skills.txt`); planned: `ocudu-review-expert` (search for correctness, 3GPP issues, life cycle issues, design smells and performance issues), `ocudu-review-concurrency` (search for concurrency issues), `ocudu-review-testing` (search for testing completion and simplicity). |
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

The box never pushes, and posts to GitLab only when started with `--glab-comment`:

- **git push is disabled.** The entrypoint rewrites every push to a network remote (`url.push-disabled://.pushInsteadOf`)
  to `image/bin/git-remote-push-disabled`, which refuses it. Fetch and clone still work, and `--no-verify` does not
  skip it. It does not cover a remote with an explicit `pushurl`.
- **Tokens stay out of the repo.** They live in `~/.config/review-box/secrets.env` (mode 600), which `review-box.sh`
  parses and passes only to the compose call. `.env` is in ocudu_tools, which every box mounts, so `review-box.sh`
  refuses to start when it sets a token. A box can still read the tokens it is given from its own environment: give it
  only tokens you can afford to leak.
- **The GitLab token is read-only.** `GITLAB_TOKEN` is the box's only GitLab credential by default; give it only the
  `read_api` scope. The deny rules below cannot cover every way to reach the API (curl, other `glab` commands), so the
  token's scope is what stops writes.
- **Claude is denied the write commands.** `image/settings.json` (installed as `~/.claude/settings.json`) denies
  `git push`, changing the push rewrite, the `glab mr` / `glab issue` subcommands that write, and `glab api` with a
  method or body. Reading MR metadata (`glab mr view`, `glab mr diff`, `glab api <GET endpoint>`) stays allowed. These
  rules apply to the commands Claude runs, not to commands scripts run for it.
- **GitLab comments are opt-in.** `--glab-comment` on `run` or `review` hands the box `GITLAB_WRITE_TOKEN` instead of
  `GITLAB_TOKEN`, after checking it has the `api` scope. Claude may then run `review-post-note <iid> <project> <file>`,
  which posts the file as a note only if its first line is the review title (`## AI review: ...`).
  `glab mr note` and every other write stay denied, and `git push` stays disabled.

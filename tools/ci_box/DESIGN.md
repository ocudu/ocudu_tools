# ci_box design

A container that runs the OCUDU AI workflows headless, in the same way in multiple places: a CI job, a GitLab Duo flow, and
a developer's machine. This document covers the box itself: how a workflow is defined and run, what it may do, and how
it is integrated in CI and Duo. Each workflow's logic lives in its skill, and is documented there
(e.g. [ci-triage](../../skills/ci-triage/SKILL.md)).

## Principles

- **One source of truth.** Skills, prompts and scripts live in ocudu_tools. CI jobs, Duo flows and developers all use
  these files. Nothing is copied by hand into another repo.
- **Same command everywhere.** `run.sh <workflow> <url> [--post]` is the only entry point. A CI job, a Duo flow and a
  developer all run it.
- **The model decides, scripts write.** The model reads GitLab and produces a result file. Once the model is done, a
  deterministic script turns that file into GitLab writes. The model never posts or edits anything itself.
- **Dry run unless told otherwise.** Nothing is written to GitLab without `--post`.
- **The trigger is swappable.** Moving from a CI job to Duo only changes how `run.sh` is started.

## Layout

```text
ocudu_tools/
  skills/                         # Claude Code skills, copied into the image
  .gitlab/ci/ci-box.yml           # CI template other projects include: .ci-box, .ci-triage
  tools/ci_box/
    DESIGN.md                     # this file
    README.md                     # quick start
    Dockerfile                    # targets: base (review later); build context: repo root
    docker-compose.yml            # developer mode: the CI image, built from the checkout
    .gitlab-ci.yml                # image build, publish and version check
    ci/version.yml                # CI_BOX_VERSION: the image tag
    bin/run.sh                    # entry point
    workflows/<workflow>/
      prompt.md                   # headless prompt; {URL} is filled in
      settings.json               # allow- and deny-list for the unattended run
      post.sh                     # collect, show and post the model's output
    results/                      # developer runs land here (git-ignored)
```

## Workflows

A workflow is a directory under `workflows/`. Adding one adds a directory. run.sh, the image and the CI template stay
as they are.

| File | Role |
| --- | --- |
| `prompt.md` | The prompt for `claude -p`. Usually "run `/<skill> {URL}` to the end", plus the unattended-run rules: never ask, don't work around a refused tool, don't write to GitLab. |
| `settings.json` | Claude Code settings for the run, mainly `permissions.allow` and `permissions.deny` (see Permissions). |
| `post.sh` | Run as `post.sh <run_dir> <since> [--post]` once the model is done. It finds the output the model wrote after `<since>`, copies it into `<run_dir>`, prints a summary, and makes the writes only with `--post`. |

Rules for a workflow:

- **The model's output is a file, checked by a script.** For example, a skill script validates the model's decisions
  before it writes the result. If there is no valid result, post.sh fails and writes nothing.
- **post.sh ignores stale output.** Only files newer than `<since>` count, so a leftover from an earlier run in the same
  box is never posted.
- **Writes are idempotent.** A re-run on the same input, or a retried job, must not post twice. For example, triage
  keys its replies by pipeline ID.
- **The writer works outside the box too.** It writes by default and takes `--dry-run`, as any CLI would. post.sh is
  what makes the box's default a dry run.

## `run.sh`

```text
run.sh <workflow> <url> [--post] [--out DIR]
```

1. **Credentials.**
   - Duo, when `AI_FLOW_AI_GATEWAY_TOKEN` is set: not implemented yet; run.sh stops.
   - Otherwise: `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN`, plus `GITLAB_TOKEN`, from the environment.
2. **Run directory.** It creates `<out>/<workflow>-<id>-<time>/`, where `<out>` is `--out`, then `AI_RESULTS_DIR`,
   then `~/results`. It touches `.started` there, which is the `<since>` mark for post.sh.
3. **Clean start.** It runs `cd $HOME`, so CI matches a developer's machine (CI starts in the project dir). It builds
   an environment from an allowlist (below).
4. **The model.** It runs
   `claude -p <prompt> --settings settings.json --permission-mode dontAsk --output-format stream-json`.
   - The run is bounded by `--max-budget-usd` (`AI_MAX_BUDGET_USD`, default 5) and the job timeout.
   - The log shows one line per tool call. The full stream is saved as `transcript.jsonl`.
   - `summary.json` gets the run's turns, cost, duration and error flag.
5. **post.sh**, in the same clean environment, with `--post` only if run.sh was given it.

A failed model run doesn't skip post.sh: post.sh is what reports that there is no result.

## Isolation

The box runs unattended with a token that can write issues. What limits it:

- **Permissions.** Under `--permission-mode dontAsk`, anything that isn't allowed is refused without a prompt (see
  Permissions).
- **Environment allowlist.** The model and post.sh get only:
  - `HOME`, `PATH`, `LANG`, `USER`, `SHELL`, `TMPDIR`;
  - the proxy variables;
  - `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN`;
  - `GITLAB_TOKEN`;
  - `ISSUE_PROJECT`.

  The rest of a CI job's environment (`CI_JOB_TOKEN`, other projects' tokens, CI variables) is dropped with
  `env -i`. A workflow that needs another variable adds it to `KEEP` in run.sh.
- **Non-root user** with a writable `HOME`. Duo runs custom images as the image's user.
- **Writes only after the model.** The writer runs from post.sh, never as a model tool. settings.json denies it.

## Permissions: skill vs box

There are two layers, with different jobs:

- **Skill `allowed-tools` (frontmatter)** is for developers. It keeps an interactive session from prompting for the
  skill's own scripts. It is part of the skill, so anyone who edits the skill can change it. It does not apply in a
  headless `dontAsk` run: every script call was refused when this was tried.
- **`workflows/<workflow>/settings.json` is the boundary for unattended runs.** It allows everything the run needs and
  nothing else. Its deny list wins over any allow. Typical entries:
  - allowed: `Skill`, the skill's scripts by full path, `jq`, and `Read`/`Edit` on the skill's work directory and on
    the skills;
  - denied: `curl`, `wget`, `glab`, `git`, `python3 -c`, the box's own scripts under `tools/`, and the web tools.

Allow rules match the literal command line. `cd X && python3 scripts/foo.py` is refused even when
`python3 /abs/path/scripts/*` is allowed. Skills therefore tell the model to call scripts by their full path and never
`cd` first.

## Image

- **Targets.** One multi-stage Dockerfile; the build context is the ocudu_tools root.
  - `base`: `python:3.12-slim` plus curl, git and jq, Claude Code (pinned with `CLAUDE_CODE_VERSION`, so a tag always
    rebuilds the same box), the skills, and the box.
  - `review` (later): `FROM base`, plus the OCUDU toolchain, clang tools and iwyu, for reviews that build.
- **Contents.** The skills and `tools/ci_box` are copied in (nothing is bind-mounted in CI or Duo). Each skill is
  linked into `~/.claude/skills/`. `bin/` is on the `PATH`.
- **Tag.** `${OCUDU_REGISTRY_SERVER}/${OCUDU_TOOLS_PROJECT}/ci-box:<CI_BOX_VERSION>`, from `tools/ci_box/ci/version.yml`.
- **Versioning.** The image carries the skills, so an image change can come from `skills/` or from `tools/ci_box/`.

## Runtimes

### CI: the template

Other projects include `.gitlab/ci/ci-box.yml` and extend its jobs:

```yaml
include:
  - project: $OCUDU_TOOLS_PROJECT
    ref: main                 # branch, tag or commit: also picks the image version
    file: .gitlab/ci/ci-box.yml
    inputs:                   # all optional
      post: false             # dry run while trying it out

ci-triage:
  extends: .ci-triage
```

- **`OCUDU_TOOLS_PROJECT`** is a project, group or instance CI/CD variable holding ocudu_tools' path. GitLab doesn't
  expand YAML-defined variables in `include`, so it can't be set in the YAML. The include uses it, and so does the
  template for its image, `${OCUDU_REGISTRY_SERVER}/${OCUDU_TOOLS_PROJECT}/ci-box`. A fork or a self-hosted copy only
  sets the variable. Leaving it unset drops the AI jobs, if the include has `rules: - if: $OCUDU_TOOLS_PROJECT`.
  ocudu_tools' own pipeline sets it to `$CI_PROJECT_PATH`.
- **`.ci-box`** runs any workflow from the job variables `AI_WORKFLOW`, `AI_URL` and `AI_POST` (`"true"` adds
  `--post`). It also sets:
  - `GIT_STRATEGY: none`, `interruptible: false`, a 1 h timeout;
  - `ci_box_results/` as artifacts, kept 30 days, also when the job fails.
- **One job per workflow** extends it with its trigger. `.ci-triage` runs in `.post`, only on schedules, `when: on_failure`.
  - With `trigger: strategy: mirror`, a failed child pipeline fails its trigger job, so child failures count.
  - A pipeline where only allowed-to-fail jobs failed passes, so it isn't triaged.
- **Version.** The template includes `version.yml` with `local:`, which resolves in ocudu_tools at the included ref. The
  include's `ref` therefore picks both the template and the image. Use `main` to follow releases, or pin a tag or
  commit.
- **Inputs:**

  | Input | Default | Use |
  | --- | --- | --- |
  | `gitlab_token_variable` | `AI_GITLAB_TOKEN` | The CI/CD variable holding the token. |
  | `post` | `true` | `false` makes `.ci-triage` a dry run. |
  | `issue_project` | `ocudu/ocudu` | Where new issues are opened. |
  | `max_budget_usd` | `5` | The model's spending limit per run. |

- **Never changes the pipeline result.** It runs after everything else, in `.post`. A failed triage shows as a failed
  `.post` job, which doesn't change what already failed.

### CI: on demand

ocudu_tools' own pipeline has a `ci-box run` job. It runs any workflow on any URL, from "Run pipeline" (web), the API,
or a trigger from another pipeline. Its inputs:

- `ai_workflow`: a workflow from `workflows/`, offered as a list;
- `ai_url`: what to run it on;
- `ai_post`: off by default.

A new workflow is added to `ai_workflow`'s options.

### Duo (planned)

The same image and command, from a Duo flow:

```yaml
image: ${OCUDU_REGISTRY_SERVER}/${OCUDU_TOOLS_PROJECT}/ci-box:<version>
injectGatewayToken: true
commands:
  - run.sh <workflow> <url-from-the-event> --post
```

Still to do or to find out:

- **Gateway credentials.** run.sh must use them: `AI_FLOW_AI_GATEWAY_TOKEN` for Claude, `AI_FLOW_GITLAB_TOKEN` for
  GitLab. The flow's service account replaces the bot.
- **The URL.** It comes from `AI_FLOW_CONTEXT`. What that holds for a pipeline event isn't documented.
- **Triggers.** Duo only starts flows on human actions, so a scheduled pipeline's "Failed" event may not count. A
  scheduled Duo trigger (beta) is the fallback. Duo disables a schedule after 3 failed runs in a row.

Until then, the CI template is the main path. Both can run side by side, because writes are idempotent.

### Developer

- **Without the box:** run the skill in Claude Code, e.g. `/ci-triage <url>`. The model's output stays local, and the
  writer can be run by hand on it.
- **With the box:** `docker compose run --rm --build ci-box ...` from `tools/ci_box/`.
  - The image is built from the checkout, so skill edits apply without a version bump.
  - The tokens are passed through from the shell. Nothing is kept in an `.env`.
  - The image takes the host uid and gid (`HOST_UID`/`HOST_GID`), so `results/` stays the developer's.

  | Command | What it does |
  | --- | --- |
  | `run.sh <workflow> <url>` | The exact CI command, as a dry run. Everything lands in `results/`. |
  | `run.sh <workflow> <url> --post` | Writes, with the developer's own token. |
  | `bash` | A shell with the skills linked. Run `claude` there and use the skills interactively. |

## Observability

Each run directory has:

- `transcript.jsonl`: the full stream of the run;
- `summary.json`: turns, cost, duration and error flag;
- the workflow's output, plus what was or would be written (e.g. `actions.json`).

The job log shows one line per tool call, the run summary, and the workflow's own summary (a table, then the list of
writes). In CI the run directory is the job artifact.

## Adding a workflow

1. **The skill** goes in `skills/`, and works interactively first. It writes its result to a file, and a script checks
   it.
2. **The workflow directory:**
   - `workflows/<name>/prompt.md`;
   - `settings.json`, allowing exactly the skill's script calls;
   - `post.sh`, built on its writer, which takes `--dry-run`.
3. **New env variables**, if any, go in `KEEP` in run.sh.
4. **The on-demand option:** add `<name>` to `ai_workflow` in ocudu_tools' `.gitlab-ci.yml`.
5. **A `.<name>` job** in `.gitlab/ci/ci-box.yml`, if it has its own trigger. It extends `.ci-box` and sets
   `AI_WORKFLOW`, `AI_URL` and its rules.
6. **The version:** bump `CI_BOX_VERSION`.

# ci_box

A container that runs the OCUDU AI workflows headless, the same way in CI, in GitLab Duo, and on a developer's
machine. Design: [DESIGN.md](DESIGN.md). For now it runs one workflow: CI triage.

## Quick start

```sh
export CLAUDE_CODE_OAUTH_TOKEN=...   # claude setup-token; or ANTHROPIC_API_KEY=...
export GITLAB_TOKEN=...              # read_api scope; api scope only to --post

docker compose run --rm --build ci-box run.sh ci-triage https://gitlab.com/ocudu/ocudu/-/pipelines/<id>          # dry run
docker compose run --rm --build ci-box run.sh ci-triage https://gitlab.com/ocudu/ocudu/-/pipelines/<id> --post   # write
docker compose run --rm --build ci-box bash                                                                    # a shell
```

Run it from this directory. If your uid/gid are not 1000, also `export HOST_UID=$(id -u) HOST_GID=$(id -g)`, so the
results stay yours.

Each run builds the image from this checkout, so skill edits apply, and runs the exact CI command, `run.sh ci-triage
<url>`. Results are in `results/<workflow>-<id>-<time>/`:

#!/usr/bin/env bash

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

# The box's only entry point: runs a workflow headless, the same way in CI and on a developer's machine.
#
#   run.sh <workflow> <url> [--post] [--out DIR]
#
# A workflow is a directory under workflows/: prompt.md (the prompt; {URL} is filled in), settings.json (allow- and
# deny-list), and post.sh, run once the model is done to collect, show and post its output (writes only with
# --post). Everything lands in <out>/<workflow>-<id>-<time>/.

set -euo pipefail

BOX=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)

die() { echo "run.sh: $*" >&2; exit 1; }

usage() {
    sed -n 's/^#   //p' "$0"
    echo "workflows: $(ls "$BOX/workflows" | tr '\n' ' ')"
    exit "${1:-0}"
}

[ $# -ge 1 ] || usage 1
workflow=$1
shift
wf="$BOX/workflows/$workflow"
[ -f "$wf/prompt.md" ] || die "unknown workflow '$workflow' (known: $(ls "$BOX/workflows" | tr '\n' ' '))"

url="" post=() out="${AI_RESULTS_DIR:-$HOME/results}"
while [ $# -gt 0 ]; do
    case "$1" in
        --post) post=(--post) ;;
        --out) out=$2; shift ;;
        -h|--help) usage ;;
        -*) die "unknown option $1" ;;
        *) [ -z "$url" ] || die "one URL only"; url=$1 ;;
    esac
    shift
done
[ -n "$url" ] || usage 1

if [ -n "${AI_FLOW_AI_GATEWAY_TOKEN:-}" ]; then
    die "Duo mode is not implemented yet"
fi
[ -n "${ANTHROPIC_API_KEY:-}${CLAUDE_CODE_OAUTH_TOKEN:-}" ] || die "set ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN"
[ -n "${GITLAB_TOKEN:-}" ] || die "set GITLAB_TOKEN"

id=$(grep -oE '[0-9]+' <<< "$url" | tail -1 || true)
run_dir="$out/${workflow}-${id:-run}-$(date -u +%Y%m%d-%H%M%S)"
mkdir -p "$run_dir"
run_dir=$(cd "$run_dir" && pwd)
echo "run.sh: $workflow $url → $run_dir" >&2

# Outputs older than this mark belong to an earlier run; post.sh ignores them.
since="$run_dir/.started"
touch "$since"

prompt=$(sed "s|{URL}|${url//|/\\|}|g" "$wf/prompt.md")

# From $HOME, as on a developer's machine: CI starts in the project dir.
cd "$HOME"

# The model and post.sh get only these variables, not the rest of the CI job's (tokens, CI_JOB_TOKEN...).
KEEP=(HOME PATH LANG USER SHELL TMPDIR HTTPS_PROXY HTTP_PROXY NO_PROXY https_proxy http_proxy no_proxy
      ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN GITLAB_TOKEN ISSUE_PROJECT)
clean_env=()
for v in "${KEEP[@]}"; do
    [ -z "${!v:-}" ] || clean_env+=("$v=${!v}")
done

# One line per tool call while it runs; the full stream is kept as the transcript.
set +e
env -i "${clean_env[@]}" claude -p "$prompt" \
    --settings "$wf/settings.json" \
    --permission-mode dontAsk \
    --output-format stream-json --verbose \
    --max-budget-usd "${AI_MAX_BUDGET_USD:-5}" \
    < /dev/null \
    | tee "$run_dir/transcript.jsonl" \
    | jq -r --unbuffered '
        select(.type == "assistant") | .message.content[]? | select(.type == "tool_use")
        | "  → \(.name) \(.input.command // .input.file_path // .input.skill // "" | tostring | .[:150])"' >&2
claude_rc=${PIPESTATUS[0]}
set -e

jq -c 'select(.type == "result") | {is_error, num_turns, total_cost_usd, duration_ms}' \
    "$run_dir/transcript.jsonl" > "$run_dir/summary.json" || true
echo "run.sh: claude exit $claude_rc, $(cat "$run_dir/summary.json" 2>/dev/null)" >&2

env -i "${clean_env[@]}" "$wf/post.sh" "$run_dir" "$since" "${post[@]}"
echo "Results: $run_dir"

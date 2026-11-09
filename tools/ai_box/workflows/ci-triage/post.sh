#!/usr/bin/env bash

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

# Last step of the ci-triage workflow, run by ai-run once the model is done:
#   post.sh <run_dir> <since> [--post]
# Takes the triage.json the run wrote (newer than <since>), shows the table, and plans the GitLab writes; makes them
# only with --post.

set -euo pipefail

HERE=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
SKILL=$HERE/../../../../skills/ci-triage/scripts
run_dir=$1 since=$2
shift 2

mapfile -t found < <(find /tmp/triage -name triage.json -newer "$since" 2>/dev/null)
if [ "${#found[@]}" -ne 1 ]; then
    echo "post.sh: expected one new triage.json under /tmp/triage, found ${#found[@]}; see $run_dir/transcript.jsonl" >&2
    exit 1
fi
cp -r "$(dirname "${found[0]}")" "$run_dir/triage"

python3 "$SKILL/build_triage.py" "$run_dir/triage" --show

python3 "$HERE/post_triage.py" "$run_dir/triage/triage.json" --actions "$run_dir/actions.json" "$@" > /dev/null
echo
echo "GitLab actions ($([ "${1:-}" = --post ] && echo "posted" || echo "dry run, not posted")):"
jq -r '.[] | "  \(.action) \(.issue)\(if .title then ": " + .title else "" end)"' "$run_dir/actions.json"

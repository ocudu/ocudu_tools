#!/usr/bin/env bash

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

# Write a guidelines review's patch: every change to the tree since setup.sh recorded the baseline, new files included.
#
#   patch.sh <results-dir>
#
# Writes <results-dir>/review.patch, or removes it when nothing changed, and prints the paths of the patch and
# report.md.

set -euo pipefail

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
source "${here}/lib.sh"

[[ $# -eq 1 ]] || die "usage: patch.sh <results-dir>"
d=$(cd "$1" && pwd) || die "no results directory $1"
load_review "${d}"
cd "${REPO}"

idx=$(worktree_index)
trap 'rm -f "${idx}"' EXIT
GIT_INDEX_FILE=${idx} git diff --cached --binary "${BASELINE}" > "${d}/review.patch"
if [[ ! -s ${d}/review.patch ]]; then
    rm -f "${d}/review.patch"
    printf 'no changes since the baseline; no patch written\n'
else
    GIT_INDEX_FILE=${idx} git diff --cached --stat "${BASELINE}"
    printf '\npatch:  %s\n' "${d}/review.patch"
fi
printf 'report: %s\n' "${d}/report.md"
# The review is done; without the ref, gc drops the baseline and the untracked files it snapshotted. BASELINE still
# resolves until then, so running this again soon after works.
git update-ref -d "refs/review/${d##*/}" 2>/dev/null || true


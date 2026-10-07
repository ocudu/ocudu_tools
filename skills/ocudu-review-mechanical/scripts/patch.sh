#!/usr/bin/env bash

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

# Write a mechanical review's patch: every change to the tree since setup.sh recorded the baseline, new files included.
#
#   patch.sh <results-dir>
#
# Writes <results-dir>/review.patch, or removes it when nothing changed, and prints the paths of the patch and
# report.md. When the results directory is a bind mount (REVIEW_RESULTS_HOST_DIR is where REVIEW_RESULTS_DIR is seen
# from outside), the paths are printed as seen from outside too.

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

if [[ -n ${REVIEW_RESULTS_HOST_DIR:-} && -n ${REVIEW_RESULTS_DIR:-} && ${d} == "${REVIEW_RESULTS_DIR%/}"/* ]]; then
    printf 'outside: %s\n' "${REVIEW_RESULTS_HOST_DIR%/}${d#"${REVIEW_RESULTS_DIR%/}"}"
fi

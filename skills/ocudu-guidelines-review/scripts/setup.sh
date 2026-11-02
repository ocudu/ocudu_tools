#!/usr/bin/env bash

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

# Set up a guidelines review: find the checkout, its build tree and the code guide, list the scope, record the tree as
# the review's baseline, and print the results directory.
#
#   setup.sh [diff [BASE] | path <dir>... | all]     default: diff
#
#   diff [BASE]    C/C++ files changed since merge-base(BASE, HEAD), uncommitted and untracked ones included;
#                  BASE defaults to $REVIEW_BASE, else origin/dev
#   path <dir>...  tracked C/C++ files under those paths
#   all            every tracked C/C++ file
#
# The checkout to review and fix is the git repo of the current directory. Where the rest is, each overridable from the
# environment:
#   OCUDU_BUILD_DIR     its CMake build tree                   default: <repo>/build
#   OCUDU_CODE_GUIDE    ocudu_docs' docs/dev_guide/code_guide  default: in an ocudu_docs checkout next to the repo
#   REVIEW_RESULTS_DIR  where review directories are made     default: ~/.cache/ocudu-review
#
# Writes <results-dir>/scope.txt and <results-dir>/review.env; the other scripts read review.env, so they take only the
# results directory. The tree as it is now (uncommitted and untracked files included) is committed, without touching
# the index or the files, as refs/review/<results-dir name>: review.patch is the diff against it, so it holds the
# review's changes and nothing else. Fails, leaving nothing behind, when the scope has no C/C++ files or the code guide
# is not found.

set -euo pipefail

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
source "${here}/lib.sh"

[[ $# -ge 1 ]] || set -- diff
scope="$*"

repo=$(git rev-parse --show-toplevel 2>/dev/null) || die "not in a git repo; run from the OCUDU checkout"
# path arguments are relative to where the user ran the skill.
start=${PWD}
cd "${repo}"

build=${OCUDU_BUILD_DIR:-${repo}/build}
guide=${OCUDU_CODE_GUIDE:-$(dirname "${repo}")/ocudu_docs/docs/dev_guide/code_guide}
[[ -f ${guide}/index.md ]] ||
    die "code guide not found at ${guide}; set OCUDU_CODE_GUIDE to ocudu_docs' docs/dev_guide/code_guide"
results_root=${REVIEW_RESULTS_DIR:-${XDG_CACHE_HOME:-${HOME}/.cache}/ocudu-review}

mode=$1
shift
base="" mb=""
case ${mode} in
    diff)
        [[ $# -le 1 ]] || die "usage: setup.sh diff [BASE]"
        base=${1:-${REVIEW_BASE:-origin/dev}}
        git rev-parse --verify -q "${base}^{commit}" >/dev/null || die "unknown base ${base}"
        mb=$(git merge-base "${base}" HEAD)
        # Deleted files have nothing left to review.
        files=$({
            git diff --name-only --diff-filter=d "${mb}"
            git ls-files --others --exclude-standard
        } | sort -u)
        ;;
    path)
        [[ $# -ge 1 ]] || die "usage: setup.sh path <dir>..."
        files=$(git -C "${start}" ls-files --full-name -- "$@")
        ;;
    all)
        files=$(git ls-files)
        ;;
    *) die "unknown scope ${mode}; use diff [BASE], path <dir>... or all" ;;
esac
files=$(grep -E "${CPP_RE}" <<<"${files}" || true)
[[ -n ${files} ]] || die "no C/C++ files in scope: ${scope}"

mkdir -p "${results_root}"
results_root=$(cd "${results_root}" && pwd)
d="${results_root}/$(date +%Y-%m-%d-%H%M)-guidelines"
if [[ -e ${d} ]]; then
    n=2
    while [[ -e "${d}.${n}" ]]; do n=$((n + 1)); done
    d="${d}.${n}"
fi
name=${d##*/}

idx=$(worktree_index)
trap 'rm -f "${idx}"' EXIT
tree=$(GIT_INDEX_FILE=${idx} git write-tree)
baseline=$(GIT_AUTHOR_NAME=ocudu-review GIT_AUTHOR_EMAIL=ocudu-review@localhost \
    GIT_COMMITTER_NAME=ocudu-review GIT_COMMITTER_EMAIL=ocudu-review@localhost \
    git commit-tree "${tree}" -p HEAD -m "ocudu-guidelines-review baseline: ${name}")
# A ref keeps the baseline from being garbage-collected while the review is open; patch.sh deletes it.
git update-ref "refs/review/${name}" "${baseline}"

mkdir "${d}"
printf '%s\n' "${files}" > "${d}/scope.txt"
{
    printf 'REPO=%s\n' "${repo}"
    printf 'BUILD=%s\n' "${build}"
    printf 'GUIDE=%s\n' "${guide}"
    printf 'SCOPE=%s\n' "${scope}"
    printf 'MODE=%s\n' "${mode}"
    printf 'BASE=%s\n' "${base}"
    printf 'MERGE_BASE=%s\n' "${mb}"
    printf 'COMMIT=%s\n' "$(git rev-parse HEAD)"
    printf 'BRANCH=%s\n' "$(git branch --show-current)"
    printf 'BASELINE=%s\n' "${baseline}"
    printf 'CREATED=%s\n' "$(date -Iseconds)"
} > "${d}/review.env"

printf 'scope: %s, %d files\n' "${scope}" "$(wc -l < "${d}/scope.txt")" >&2
if [[ -n $(git status --porcelain) ]]; then
    printf 'note: %s has uncommitted changes; the fixes go on top of them, and review.patch holds only the fixes\n' \
        "${repo}" >&2
fi
printf '%s\n' "${d}"

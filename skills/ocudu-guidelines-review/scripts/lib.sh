# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

# Sourced by this skill's shell scripts.

CPP_RE='\.(h|hh|hpp|c|cc|cpp|cxx|ipp|tpp)$'
SRC_RE='\.(c|cc|cpp|cxx)$'

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# Reads <results-dir>/review.env, written by setup.sh, into shell variables of the same names (REPO, BUILD, ...).
load_review() {
    local env="$1/review.env" key value
    [[ -r ${env} ]] || die "${env} not found; run setup.sh first"
    while IFS='=' read -r key value; do
        [[ ${key} =~ ^[A-Z_]+$ ]] || continue
        printf -v "${key}" '%s' "${value}"
    done < "${env}"
}

# Prints the path of a throwaway index that holds the whole working tree (tracked changes and untracked files, minus
# ignored ones): what `git add -A` would stage, without touching the user's index. The caller removes it.
worktree_index() {
    local idx real
    idx=$(mktemp)
    real=$(git rev-parse --git-path index)
    if [[ -f ${real} ]]; then
        # Starting from the real index keeps its stat cache, so only changed files are hashed.
        cp "${real}" "${idx}"
    else
        rm -f "${idx}"
    fi
    GIT_INDEX_FILE=${idx} git add -A
    printf '%s\n' "${idx}"
}

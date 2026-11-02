#!/usr/bin/env bash

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

# Validate a guidelines review's edits: format them, build what they touch, run the tests that cover them.
#
#   validate.sh <results-dir>
#
# The edited files are the tree's C/C++ changes since the baseline setup.sh recorded, out-of-scope files included.
#   format  diff scope: clang-format-diff-21 over the lines changed since the merge-base in the edited files;
#           other scopes: clang-format-21 over the edited files whole. $CLANG_FORMAT / $CLANG_FORMAT_DIFF override.
#   build   the CMake targets of every edited source file and of every source file that includes an edited header
#           (`cmake --build`, so any generator works).
#   test    ctest over the tests run by the test executables among those targets.
# Writes <results-dir>/validation/{format,build,test}.log and summary.md (the report's Validation row). Exits 1 when
# formatting, the build or a test fails.

set -euo pipefail

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
source "${here}/lib.sh"

[[ $# -eq 1 ]] || die "usage: validate.sh <results-dir>"
d=$(cd "$1" && pwd) || die "no results directory $1"
load_review "${d}"

out="${d}/validation"
mkdir -p "${out}"
db="${BUILD}/compile_commands.json"
[[ -f ${db} ]] || die "no ${db}; tool_checks.py configures the build tree"
command -v jq >/dev/null || die "jq not found"
cd "${REPO}"

clang_format=${CLANG_FORMAT:-clang-format-21}
clang_format_diff=${CLANG_FORMAT_DIFF:-clang-format-diff-21}

# The report's row, also printed; returns 1 for a failure.
finish() {
    local result=$1
    shift
    {
        printf '| Validation (build + tests) | %s | – | – |\n\n' "${result}"
        printf '%s\n' "$@"
    } | tee "${out}/summary.md"
    printf '\nwritten: %s/summary.md\n' "${out}"
    [[ ${result} != failed* ]]
}

as_json() { jq -R . | jq -s .; }

idx=$(worktree_index)
trap 'rm -f "${idx}"' EXIT

mapfile -t edited < <(GIT_INDEX_FILE=${idx} git diff --cached --name-only --diff-filter=d "${BASELINE}" |
    grep -E "${CPP_RE}" || true)
if [[ ${#edited[@]} -eq 0 ]]; then
    finish "ok: no C/C++ file edited"
    exit
fi

# --- format ----------------------------------------------------------------------------------------------------------

before=$(sha1sum -- "${edited[@]}")
if [[ ${MODE} == diff ]]; then
    # The throwaway index holds the whole tree, so new files are in the diff too.
    printf '$ git diff -U0 %s -- <edited files> | %s -p1 -i\n' "${MERGE_BASE}" "${clang_format_diff}" > "${out}/format.log"
    GIT_INDEX_FILE=${idx} git diff --cached -U0 --no-color "${MERGE_BASE}" -- "${edited[@]}" |
        "${clang_format_diff}" -p1 -i >> "${out}/format.log" 2>&1 ||
        { finish "failed: formatting, see validation/format.log"; exit 1; }
else
    printf '$ %s -i <edited files>\n' "${clang_format}" > "${out}/format.log"
    "${clang_format}" -i -- "${edited[@]}" >> "${out}/format.log" 2>&1 ||
        { finish "failed: formatting, see validation/format.log"; exit 1; }
fi
mapfile -t formatted < <(diff <(printf '%s\n' "${before}") <(sha1sum -- "${edited[@]}") | sed -n 's/^> [0-9a-f]*  //p')

# --- build -----------------------------------------------------------------------------------------------------------

# Translation units: the edited sources, and every source that includes an edited header, directly or through other
# headers (by file name, so it may build a few more than needed, never fewer).
tus=()
declare -A seen=()
frontier=()
for f in "${edited[@]}"; do
    if [[ ${f} =~ ${SRC_RE} ]]; then
        tus+=("${REPO}/${f}")
        continue
    fi
    name=$(basename "${f}")
    [[ -n ${seen[${name}]:-} ]] || { seen[${name}]=1; frontier+=("${name}"); }
done
# Each round finds the files including a header of the last round; the headers among them make the next round.
while [[ ${#frontier[@]} -gt 0 ]]; do
    names=$(printf '%s\n' "${frontier[@]}" | sed 's/[][\.*^$+?(){}|/]/\\&/g' | paste -sd'|')
    frontier=()
    while read -r f; do
        [[ -n ${f} ]] || continue
        if [[ ${f} =~ ${SRC_RE} ]]; then
            tus+=("${REPO}/${f}")
            continue
        fi
        name=$(basename "${f}")
        [[ -n ${seen[${name}]:-} ]] || { seen[${name}]=1; frontier+=("${name}"); }
    done < <(git grep --untracked -lE "#[[:space:]]*include[[:space:]]*[<\"]([^>\"]*/)?(${names})[>\"]" \
        -- lib apps tests include | grep -E "${CPP_RE}" || true)
done

# Each object's CMake target is in its output path: <dir>/CMakeFiles/<target>.dir/<source>.o.
mapfile -t objects < <(printf '%s\n' "${tus[@]}" | sort -u | as_json |
    jq -r --slurpfile db "${db}" '. as $tus | $db[0][] | select(.file as $f | $tus | index($f)) | .output // empty' |
    sort -u)
mapfile -t targets < <(printf '%s\n' "${objects[@]}" | sed -nE 's#(.*/)?CMakeFiles/([^/]+)\.dir/.*#\2#p' | sort -u)
mapfile -t test_targets < <(printf '%s\n' "${objects[@]}" | grep '^tests/' |
    sed -nE 's#.*/CMakeFiles/([^/]+)\.dir/.*#\1#p' | sort -u || true)

details=(
    "Edited files: ${#edited[@]}; reformatted: ${#formatted[@]}${formatted[*]:+ (${formatted[*]})}"
    "Targets built: ${#targets[@]}; test executables: ${#test_targets[@]}${test_targets[*]:+ (${test_targets[*]})}"
)

if [[ ${#targets[@]} -eq 0 ]]; then
    finish "failed: no target in compile_commands.json builds the edited files" "${details[@]}"
    exit 1
fi
if ! cmake --build "${BUILD}" --parallel --target "${targets[@]}" > "${out}/build.log" 2>&1; then
    tail -n 40 "${out}/build.log" >&2
    finish "failed: build, see validation/build.log" "${details[@]}"
    exit 1
fi

# --- test ------------------------------------------------------------------------------------------------------------

if [[ ${#test_targets[@]} -eq 0 ]]; then
    finish "ok: built ${#targets[@]} targets; no test executable includes an edited file" "${details[@]}"
    exit
fi

# Tests are matched by the executable they run, so gtest-discovered tests (one per case) are all picked up. Discovery
# happens at build time, hence after the build.
mapfile -t tests < <(ctest --test-dir "${BUILD}" --show-only=json-v1 |
    jq -r --argjson ts "$(printf '%s\n' "${test_targets[@]}" | as_json)" \
        '.tests[] | select((.command[0] // "") | split("/") | last | IN($ts[])) | .name')
if [[ ${#tests[@]} -eq 0 ]]; then
    finish "ok: built ${#targets[@]} targets; the test executables register no ctest test" "${details[@]}"
    exit
fi

regex="^($(printf '%s\n' "${tests[@]}" | sed 's/[][\.*^$+?(){}|/]/\\&/g' | paste -sd'|'))\$"
if ctest --test-dir "${BUILD}" -R "${regex}" --output-on-failure > "${out}/test.log" 2>&1; then
    finish "ok: built ${#targets[@]} targets; $(grep -E 'tests passed' "${out}/test.log" | tail -n 1)" "${details[@]}"
else
    grep -A 1000 'The following tests FAILED' "${out}/test.log" >&2 || tail -n 40 "${out}/test.log" >&2
    finish "failed: tests, see validation/test.log; $(grep -E 'tests passed' "${out}/test.log" | tail -n 1)" \
        "${details[@]}"
    exit 1
fi

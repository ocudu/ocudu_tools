#!/usr/bin/env bash

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

# Run `git diff <args>` and prefix every line with its line number in the new
# file, so findings cite real file lines rather than positions in the diff.
#
# Usage: numbered_diff.sh <git diff arguments>
#
# Output columns: `<new line> <diff line>` for added and context lines,
# `    - <diff line>` for removed lines (they have no new-file line), headers
# unchanged.

set -euo pipefail

git diff --no-color "$@" | awk '
    # File headers sit between `diff --git` and the first hunk.
    /^diff --git / { in_hunk = 0; print; next }
    !in_hunk && !/^@@ / { print; next }
    /^@@ / {
        # @@ -a,b +c,d @@: the next new-side line is c.
        split($3, plus, ",")
        n = substr(plus[1], 2) + 0
        in_hunk = 1
        print
        next
    }
    /^\+/ { printf "%5d %s\n", n++, $0; next }
    /^-/  { printf "    - %s\n", $0; next }
    /^\\/ { printf "      %s\n", $0; next }
    { printf "%5d %s\n", n++, $0 }
'

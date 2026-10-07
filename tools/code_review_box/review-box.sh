#!/usr/bin/env bash

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

# Manage OCUDU code review box containers.
#
#   review-box.sh run [--ref <ref>] <name> [command...]
#                                           build if needed, then enter a review box
#   review-box.sh build                     (re)build the image
#   review-box.sh ls                        running boxes, images, volumes
#   review-box.sh rm <name>...              remove those containers (results are kept)
#   review-box.sh prune [--yes]             remove containers, images and volumes

set -euo pipefail

BOX_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="io.srs.review-box=true"
PROJECT_LABEL="com.docker.compose.project=review-box"
COMPOSE=(docker compose --project-directory "${BOX_DIR}" -f "${BOX_DIR}/docker-compose.yml")

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '8,13s/^# \{0,1\}//p' "$0" >&2; exit 1; }

export HOST_UID="$(id -u)" HOST_GID="$(id -g)"

# A box is usually named after a branch, but the name becomes the container name, which
# Docker restricts to [a-zA-Z0-9][a-zA-Z0-9_.-]*: feature/foo becomes feature-foo.
box_name() {
    local name
    name=$(printf '%s' "$1" | tr -c 'a-zA-Z0-9_.-' '-' | sed 's/^[^a-zA-Z0-9]*//')
    [[ -n "${name}" ]] || die "invalid name '$1'"
    printf '%s\n' "${name}"
}

cmd_build() {
    [[ -f "${BOX_DIR}/.env" ]] || die "missing ${BOX_DIR}/.env (cp .env.example .env)"
    "${COMPOSE[@]}" build review-box
}

cmd_run() {
    # The workspace is made on start from the checkout's HEAD and uncommitted changes, or
    # from this ref (a branch, a commit, origin/<mr-branch>) when given.
    if [[ ${1:-} == --ref ]]; then
        [[ $# -ge 2 ]] || usage
        export REVIEW_REF=$2
        shift 2
    fi
    [[ $# -ge 1 ]] || usage
    local name
    name=$(box_name "$1")
    [[ "${name}" == "$1" ]] || printf 'box name: %s\n' "${name}" >&2
    shift
    if docker ps -a --format '{{.Names}}' | grep -qx "${name}"; then
        die "container '${name}' already exists ($(basename "$0") rm ${name})"
    fi

    # Docker would create a directory in place of a missing bind-mount source.
    [[ -f "${HOME}/.gitconfig" ]] || die "missing ${HOME}/.gitconfig"
    [[ -d "${HOME}/.claude" ]] || die "missing ${HOME}/.claude"
    [[ -f "${HOME}/.claude.json" ]] || die "missing ${HOME}/.claude.json"
    [[ -n "${SSH_AUTH_SOCK:-}" ]] || printf 'warning: no ssh agent; cloning over ssh will fail\n' >&2
    mkdir -p "${HOME}/.cache/ccache"

    # Empty result directories from earlier throwaway runs are noise; rmdir never removes
    # one with anything in it. A running box's directory is its bind-mount source, so
    # removing it would leave that box writing into a deleted directory.
    local dir running
    running=$(docker ps --format '{{.Names}}')
    for dir in "${BOX_DIR}"/results/*/; do
        grep -qxF "$(basename "${dir}")" <<<"${running}" || rmdir "${dir}" 2>/dev/null || true
    done
    mkdir -p "${BOX_DIR}/results/${name}"

    export REVIEW_BOX_NAME="${name}"
    export REVIEW_BOX_HOST_RESULTS="${BOX_DIR}/results/${name}"

    cmd_build
    exec "${COMPOSE[@]}" run --rm --name "${name}" review-box "$@"
}

cmd_ls() {
    printf '\ncontainers\n'
    docker ps --filter "label=${PROJECT_LABEL}" --format 'table {{.Names}}\t{{.Status}}'
    printf '\nimages\n'
    docker images --filter "label=${LABEL}" --format 'table {{.Repository}}:{{.Tag}}\t{{.ID}}\t{{.Size}}'
    printf '\nvolumes\n'
    docker volume ls --filter "label=${PROJECT_LABEL}" --format '{{.Name}}'
    printf '\nresults\n'
    ls -1 "${BOX_DIR}/results" 2>/dev/null | grep -v '^\.gitkeep$' || true
}

cmd_rm() {
    [[ $# -ge 1 ]] || usage
    local name
    for name in "$@"; do
        name=$(box_name "${name}")
        if docker ps -a --filter "label=${PROJECT_LABEL}" --format '{{.Names}}' | grep -qx "${name}"; then
            docker rm -f "${name}" >/dev/null && printf 'removed container %s\n' "${name}"
        else
            printf 'no container %s\n' "${name}" >&2
        fi
        rmdir "${BOX_DIR}/results/${name}" 2>/dev/null || true
    done
}

cmd_prune() {
    if [[ ${1:-} != --yes ]]; then
        read -r -p "Remove all review-box containers, images and volumes (results are kept)? [y/N] " ans
        [[ ${ans} == [yY]* ]] || exit 0
    fi
    docker ps -aq --filter "label=${PROJECT_LABEL}" | xargs -r docker rm -f >/dev/null
    docker images -q --filter "label=${LABEL}" | sort -u | xargs -r docker rmi -f >/dev/null
    docker volume ls -q --filter "label=${PROJECT_LABEL}" | xargs -r docker volume rm >/dev/null
    printf 'pruned\n'
}

[[ $# -ge 1 ]] || usage
cmd=$1; shift
case ${cmd} in
    run) cmd_run "$@" ;;
    build) cmd_build "$@" ;;
    ls) cmd_ls "$@" ;;
    rm) cmd_rm "$@" ;;
    prune) cmd_prune "$@" ;;
    *) usage ;;
esac

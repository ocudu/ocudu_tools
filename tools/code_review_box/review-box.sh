#!/usr/bin/env bash

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

# Manage OCUDU code review box containers.
#
#   review-box.sh run [--ref <ref>] <name> [command...]
#                                           build if needed, then enter a review box
#   review-box.sh review --mr <iid|url> [--prompt <file>] [--post]
#                                           unattended review of a GitLab MR; prints the report
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
usage() { sed -n '8,15s/^# \{0,1\}//p' "$0" >&2; exit 1; }

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

# Checks the host, prepares the results directory of box <name> and builds the image.
prepare_box() {
    local name=$1
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
    prepare_box "${name}"
    exec "${COMPOSE[@]}" run --rm --name "${name}" review-box "$@"
}

# Unattended: claude -p with the allow-list of image/headless-settings.json, so a tool not on it is refused rather
# than asked for. The box only reads GitLab; the report is posted from the host.
cmd_review() {
    local mr="" prompt_file="${BOX_DIR}/prompts/mr-review.md" post=0
    while [[ $# -gt 0 ]]; do
        case $1 in
            --mr) mr=${2:?}; shift 2 ;;
            --prompt) prompt_file=${2:?}; shift 2 ;;
            --post) post=1; shift ;;
            *) usage ;;
        esac
    done
    [[ -n "${mr}" ]] || usage
    [[ -r "${prompt_file}" ]] || die "cannot read prompt ${prompt_file}"
    command -v glab >/dev/null || die "glab not found"
    command -v jq >/dev/null || die "jq not found"

    local host=gitlab.com project=${REVIEW_MR_PROJECT:-ocudu/ocudu} iid
    if [[ ${mr} =~ ^https?://([^/]+)/(.+)/-/merge_requests/([0-9]+) ]]; then
        host=${BASH_REMATCH[1]} project=${BASH_REMATCH[2]} iid=${BASH_REMATCH[3]}
    elif [[ ${mr} =~ ^!?([0-9]+)$ ]]; then
        iid=${BASH_REMATCH[1]}
    else
        die "invalid MR ${mr}; give its number or URL"
    fi
    local info url target
    info=$(glab mr view "${iid}" -R "${host}/${project}" -F json) || die "cannot read MR ${iid} of ${project}"
    url=$(jq -r .web_url <<<"${info}")
    target=$(jq -r .target_branch <<<"${info}")

    local name prompt report
    name=$(box_name "mr-${iid}")
    # REVIEW_REF from .env would clash with the MR.
    export REVIEW_MR=${iid} REVIEW_BASE="origin/${target}" REVIEW_REF=""
    prompt=$(sed -e "s|{MR_URL}|${url}|g" -e "s|{BASE}|${REVIEW_BASE}|g" "${prompt_file}")
    prepare_box "${name}"
    report="${BOX_DIR}/results/${name}/$(date +%Y-%m-%d-%H%M)-report.md"
    printf 'reviewing %s (base %s)\n' "${url}" "${REVIEW_BASE}" >&2
    "${COMPOSE[@]}" run --rm -T --name "${name}" review-box \
        claude -p "${prompt}" --settings /opt/review-box/headless-settings.json > "${report}" ||
        die "review failed; partial output in ${report}"
    [[ -s "${report}" ]] || die "empty report: ${report}"
    cat "${report}"
    printf '\nreport: %s\n' "${report}" >&2

    if [[ ${post} -eq 1 ]]; then
        local body
        body=$(printf 'Claude says:\n'; sed 's/^/> /' "${report}")
        glab mr note "${iid}" -R "${host}/${project}" -m "${body}" >/dev/null ||
            die "posting failed; report kept in ${report}"
        printf 'posted on %s\n' "${url}" >&2
    fi
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
    review) cmd_review "$@" ;;
    build) cmd_build "$@" ;;
    ls) cmd_ls "$@" ;;
    rm) cmd_rm "$@" ;;
    prune) cmd_prune "$@" ;;
    *) usage ;;
esac

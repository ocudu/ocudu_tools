#!/usr/bin/env bash

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

# Manage OCUDU code review box containers.
#
#   review-box.sh run [--glab-comment] [--ref <ref>] <name> [command...]
#                                           build if needed, then enter a review box
#   review-box.sh review --mr <iid|url> [--prompt <file>] [--post] [--glab-comment]
#                                           unattended review of a GitLab MR; prints the report
#   --glab-comment lets Claude post MR notes, with GITLAB_WRITE_TOKEN (api scope)
#   review-box.sh build                     (re)build the image
#   review-box.sh ls                        running boxes, images, volumes
#   review-box.sh rm <name>...              remove those containers (results are kept)
#   review-box.sh prune [--yes]             remove your containers and volumes, and the image

set -euo pipefail

BOX_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="io.srs.review-box=true"
# Containers and volumes are per user; ls, rm and prune touch only the caller's.
USER_LABEL="io.srs.review-box.uid=$(id -u)"
# Container names are global to the Docker daemon, so box <name> runs as container <name>.<user>.
CONTAINER_SUFFIX=$(id -un)
CONTAINER_SUFFIX=".${CONTAINER_SUFFIX//[^a-zA-Z0-9_.-]/-}"
COMPOSE=(docker compose --project-directory "${BOX_DIR}" -f "${BOX_DIR}/docker-compose.yml")

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '8,16s/^# \{0,1\}//p' "$0" >&2; exit 1; }

export HOST_UID="$(id -u)" HOST_GID="$(id -g)"

# The skills the box runs live in the ocudu_tools checkout that holds this script, so it is mounted unless the
# environment or .env says otherwise (empty means clone). The shell environment overrides .env for compose, so this is
# only set when neither has it.
if [[ ! -v SRC_OCUDU_TOOLS ]] && ! grep -qE '^[[:space:]]*SRC_OCUDU_TOOLS=' "${BOX_DIR}/.env" 2>/dev/null; then
    export SRC_OCUDU_TOOLS="$(cd "${BOX_DIR}/../.." && pwd)"
fi

# Tokens live outside the repo: .env sits in ocudu_tools, which the box mounts, so a token there is readable by Claude
# in every box. The file is parsed, never sourced.
SECRETS_FILE=${REVIEW_BOX_SECRETS:-${XDG_CONFIG_HOME:-${HOME}/.config}/review-box/secrets.env}
SECRET_NAMES=(CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY GITLAB_TOKEN GITLAB_WRITE_TOKEN)

# Exports the secrets of SECRETS_FILE that the environment does not already set. The values reach only the compose
# call; compose passes the box the ones docker-compose.yml lists.
load_secrets() {
    [[ -e "${SECRETS_FILE}" ]] || return 0
    [[ -O "${SECRETS_FILE}" ]] || die "${SECRETS_FILE} is not owned by you"
    local mode line name value
    mode=$(stat -c %a "${SECRETS_FILE}")
    [[ ${mode} == [4-6]00 ]] || die "${SECRETS_FILE} is mode ${mode}; chmod 600 it"
    while IFS= read -r line || [[ -n ${line} ]]; do
        [[ ${line} =~ ^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
        name=${BASH_REMATCH[2]} value=${BASH_REMATCH[3]}
        [[ " ${SECRET_NAMES[*]} " == *" ${name} "* ]] || die "${SECRETS_FILE}: unknown variable ${name}"
        [[ ${value} =~ ^\"(.*)\"$ || ${value} =~ ^\'(.*)\'$ ]] && value=${BASH_REMATCH[1]}
        [[ -v ${name} ]] || export "${name}=${value}"
    done < "${SECRETS_FILE}"
}

# .env is in the repo and mounted into every box: refuse anything that looks like a secret there.
check_env_file() {
    local found
    local re='^[[:space:]]*(export[[:space:]]+)?([A-Za-z0-9_]*(TOKEN|API_KEY|SECRET|PASSWORD)[A-Za-z0-9_]*)=.*'
    found=$(sed -nE "s/${re}/\\2/p" "${BOX_DIR}/.env" | sort -u | paste -sd' ')
    [[ -z "${found}" ]] || die ".env sets ${found}; move it to ${SECRETS_FILE} (mode 600)"
}

# GitLab comments: the box gets GITLAB_WRITE_TOKEN in place of the read token, once its scopes are checked.
enable_glab_comment() {
    local token=${GITLAB_WRITE_TOKEN:-} host=${1:-gitlab.com} self scopes
    [[ -n "${token}" ]] || die "--glab-comment needs GITLAB_WRITE_TOKEN in ${SECRETS_FILE} or the environment"
    self=$(curl -fsS -H "PRIVATE-TOKEN: ${token}" "https://${host}/api/v4/personal_access_tokens/self") ||
        die "cannot read the scopes of GITLAB_WRITE_TOKEN from ${host}"
    scopes=$(jq -r '.scopes // [] | join(" ")' <<<"${self}")
    [[ " ${scopes} " == *" api "* ]] || die "GITLAB_WRITE_TOKEN lacks the api scope (has: ${scopes:-none})"
    export GITLAB_TOKEN=${token} REVIEW_BOX_GLAB_COMMENT=1
    printf 'GitLab comments enabled: Claude may post MR notes\n' >&2
}

# A box is usually named after a branch, but the name goes into the container name, which
# Docker restricts to [a-zA-Z0-9][a-zA-Z0-9_.-]*: feature/foo becomes feature-foo.
box_name() {
    local name
    name=$(printf '%s' "$1" | tr -c 'a-zA-Z0-9_.-' '-' | sed 's/^[^a-zA-Z0-9]*//')
    [[ -n "${name}" ]] || die "invalid name '$1'"
    printf '%s\n' "${name}"
}

cmd_build() {
    [[ -f "${BOX_DIR}/.env" ]] || die "missing ${BOX_DIR}/.env (cp .env.example .env)"
    check_env_file
    "${COMPOSE[@]}" build review-box
}

# The names of the caller's boxes, running or not with -a.
box_names() {
    local container
    docker ps "$@" --filter "label=${USER_LABEL}" --format '{{.Names}}' | while read -r container; do
        printf '%s\n' "${container%"${CONTAINER_SUFFIX}"}"
    done
}

# Checks the host, prepares the results directory of box <name> and builds the image.
prepare_box() {
    local name=$1
    if docker ps -a --format '{{.Names}}' | grep -qxF "${name}${CONTAINER_SUFFIX}"; then
        die "box '${name}' already exists ($(basename "$0") rm ${name})"
    fi

    # Docker would create a directory in place of a missing bind-mount source.
    [[ -f "${HOME}/.gitconfig" ]] || die "missing ${HOME}/.gitconfig"
    [[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}${ANTHROPIC_API_KEY:-}" ]] ||
        die "no Claude token: run \`claude setup-token\` and add CLAUDE_CODE_OAUTH_TOKEN=<token> to ${SECRETS_FILE}"
    # `claude setup-token` prints a login URL before the token; the URL is easy to paste by mistake.
    [[ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" || ${CLAUDE_CODE_OAUTH_TOKEN} == sk-ant-oat* ]] ||
        die "CLAUDE_CODE_OAUTH_TOKEN in ${SECRETS_FILE} is not a token (it starts with sk-ant-oat)"
    [[ -n "${SSH_AUTH_SOCK:-}" ]] || printf 'warning: no ssh agent; cloning over ssh will fail\n' >&2
    mkdir -p "${HOME}/.cache/ccache"

    # Empty result directories from earlier throwaway runs are noise; rmdir never removes
    # one with anything in it. A running box's directory is its bind-mount source, so
    # removing it would leave that box writing into a deleted directory.
    local dir running
    running=$(box_names)
    for dir in "${BOX_DIR}"/results/*/; do
        grep -qxF "$(basename "${dir}")" <<<"${running}" || rmdir "${dir}" 2>/dev/null || true
    done
    mkdir -p "${BOX_DIR}/results/${name}"

    export REVIEW_BOX_NAME="${name}"
    # Host paths stay on the host: the box names result files relative to ~/results, which is this directory.
    printf 'results: %s\n' "${BOX_DIR}/results/${name}" >&2

    # stdout is the review's report in `review`.
    cmd_build >&2
}

cmd_run() {
    load_secrets
    if [[ ${1:-} == --glab-comment ]]; then
        enable_glab_comment
        shift
    fi
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
    exec "${COMPOSE[@]}" run --rm --label "${USER_LABEL}" --name "${name}${CONTAINER_SUFFIX}" review-box "$@"
}

# Unattended: claude -p with the allow-list of image/headless-settings.json, so a tool not on it is refused rather
# than asked for. The box only reads GitLab; the report is posted from the host.
cmd_review() {
    load_secrets
    local mr="" prompt_file="${BOX_DIR}/prompts/mr-review.md" post=0 comment=0
    while [[ $# -gt 0 ]]; do
        case $1 in
            --mr) mr=${2:?}; shift 2 ;;
            --prompt) prompt_file=${2:?}; shift 2 ;;
            --post) post=1; shift ;;
            --glab-comment) comment=1; shift ;;
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
    [[ ${comment} -eq 0 ]] || enable_glab_comment "${host}"
    local info url target title
    info=$(glab mr view "${iid}" -R "${host}/${project}" -F json) || die "cannot read MR ${iid} of ${project}"
    url=$(jq -r .web_url <<<"${info}")
    target=$(jq -r .target_branch <<<"${info}")
    title=$(jq -r .title <<<"${info}")

    local name prompt report
    name=$(box_name "mr-${iid}")
    # REVIEW_REF from .env would clash with the MR.
    export REVIEW_MR=${iid} REVIEW_BASE="origin/${target}" REVIEW_REF=""
    prompt=$(sed -e "s|{MR_URL}|${url}|g" -e "s|{BASE}|${REVIEW_BASE}|g" -e "s|{IID}|${iid}|g" \
        -e "s|{PROJECT}|${project}|g" "${prompt_file}")
    prepare_box "${name}"
    report="${BOX_DIR}/results/${name}/$(date +%Y-%m-%d-%H%M)-report.md"
    printf 'reviewing %s (base %s)\n' "${url}" "${REVIEW_BASE}" >&2
    "${COMPOSE[@]}" run --rm -T --label "${USER_LABEL}" --name "${name}${CONTAINER_SUFFIX}" review-box \
        claude -p "${prompt}" --settings /home/dev/.claude/headless-settings.json > "${report}" ||
        die "review failed; partial output in ${report}"
    [[ -s "${report}" ]] || die "empty report: ${report}"
    cat "${report}"
    printf '\nreport: %s\n' "${report}" >&2

    if [[ ${post} -eq 1 ]]; then
        local body
        # The prompt asks for the title line first; a report without one gets it from the MR.
        if [[ $(head -n 1 "${report}") =~ ^#{0,6}[[:space:]]*AI\ review: ]]; then
            body=$(cat "${report}")
        else
            body=$(printf '## AI review: %s\n\n' "${title}"; cat "${report}")
        fi
        glab mr note "${iid}" -R "${host}/${project}" -m "${body}" >/dev/null ||
            die "posting failed; report kept in ${report}"
        printf 'posted on %s\n' "${url}" >&2
    fi
}

cmd_ls() {
    printf '\nboxes\n'
    box_names
    printf '\nimages\n'
    docker images --filter "label=${LABEL}" --format 'table {{.Repository}}:{{.Tag}}\t{{.ID}}\t{{.Size}}'
    printf '\nvolumes\n'
    docker volume ls --filter "label=${USER_LABEL}" --format '{{.Name}}'
    printf '\nresults\n'
    ls -1 "${BOX_DIR}/results" 2>/dev/null | grep -v '^\.gitkeep$' || true
}

cmd_rm() {
    [[ $# -ge 1 ]] || usage
    local name
    for name in "$@"; do
        name=$(box_name "${name}")
        if box_names -a | grep -qxF "${name}"; then
            # -v: the anonymous volumes of cloned repos go with the box, as with `run --rm`.
            docker rm -f -v "${name}${CONTAINER_SUFFIX}" >/dev/null && printf 'removed box %s\n' "${name}"
        else
            printf 'no box %s\n' "${name}" >&2
        fi
        rmdir "${BOX_DIR}/results/${name}" 2>/dev/null || true
    done
}

cmd_prune() {
    if [[ ${1:-} != --yes ]]; then
        read -r -p "Remove your review-box containers and volumes, and the image (results are kept)? [y/N] " ans
        [[ ${ans} == [yY]* ]] || exit 0
    fi
    docker ps -aq --filter "label=${USER_LABEL}" | xargs -r docker rm -f -v >/dev/null
    docker images -q --filter "label=${LABEL}" | sort -u | xargs -r docker rmi -f >/dev/null
    docker volume ls -q --filter "label=${USER_LABEL}" | xargs -r docker volume rm >/dev/null
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

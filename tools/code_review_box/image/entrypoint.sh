#!/usr/bin/env bash

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

# Prepares the review box on every start: Claude config, git, repos, skills.

set -euo pipefail

REPOS_DIR="${HOME}/repos"
PROJECT_DIR="${HOME}/review"
CONF_DIR=/opt/review-box
MANIFEST="${HOME}/box.yml"
# Repos the box knows about. Each is mounted when SRC_<NAME> is set, else cloned from CLONE_<NAME> (at REF_<NAME>) into
# the container and discarded when it exits.
BOX_REPOS="ocudu ocudu_docs 3gpp_md ocudu_tools"

REPO_FACTS=()

log() { printf '[review-box] %s\n' "$*" >&2; }

# Docker creates missing bind-mount parents as root; hand them back.
fix_owner() {
    [[ -e "$1" && ! -O "$1" ]] && sudo chown "$(id -u):$(id -g)" "$1" || true
}

# Host Claude config is mounted read-only and copied in, so token refreshes and session state stay in the container's
# volume and the host is never written to.
seed_claude_config() {
    fix_owner "${HOME}/.claude"
    if [[ -r /host-claude/.credentials.json && ! -e "${HOME}/.claude/.credentials.json" ]]; then
        install -m 600 /host-claude/.credentials.json "${HOME}/.claude/.credentials.json"
        log "seeded Claude credentials from host"
    fi
    if [[ -r /host-claude.json && ! -e "${HOME}/.claude.json" ]]; then
        install -m 600 /host-claude.json "${HOME}/.claude.json"
    fi
    # Everything Claude needs is in the project's CLAUDE.md; drop the user memory that earlier images left in the
    # persistent ~/.claude volume.
    rm -f "${HOME}/.claude/CLAUDE.md"

    # The project ships with the box, so there is nothing for the user to vet.
    local config="${HOME}/.claude.json" tmp
    [[ -s "${config}" ]] || echo '{}' > "${config}"
    tmp=$(mktemp)
    jq --arg p "${PROJECT_DIR}" '.projects[$p].hasTrustDialogAccepted = true' \
        "${config}" > "${tmp}" && install -m 600 "${tmp}" "${config}"
    rm -f "${tmp}"
}

seed_git() {
    [[ -r /host-gitconfig ]] && install -m 644 /host-gitconfig "${HOME}/.gitconfig"
    # The host's signing key is not here, and the failure reads as "failed to write commit object", which points nowhere
    # near it.
    git config --global commit.gpgsign false
    git config --global tag.gpgsign false
    git config --global --add safe.directory '*'
    # A review hands back a patch; nothing leaves the container through git. Every push to a network remote is
    # rewritten to the push-disabled:// helper (image/bin/git-remote-push-disabled), which refuses it. Fetch and clone
    # are untouched, and unlike a pre-push hook this is not skipped by --no-verify.
    git config --global --unset-all url.push-disabled://.pushInsteadOf || true
    local prefix
    for prefix in git@ ssh:// https:// http:// git://; do
        git config --global --add url.push-disabled://.pushInsteadOf "${prefix}"
    done

    mkdir -p "${HOME}/.ssh" && chmod 700 "${HOME}/.ssh"
    if [[ ! -s "${HOME}/.ssh/known_hosts" ]]; then
        ssh-keyscan -t rsa,ecdsa,ed25519 gitlab.com github.com 2>/dev/null \
            > "${HOME}/.ssh/known_hosts" || true
    fi
    if [[ -n "${SSH_AUTH_SOCK:-}" && ! -S "${SSH_AUTH_SOCK}" ]]; then
        log "warning: no ssh agent; cloning and fetching over ssh will fail"
    fi
}

bootstrap_repo() {
    local name=$1 key=${1^^}
    local src_var="SRC_${key}" url_var="CLONE_${key}" ref_var="REF_${key}"
    local src=${!src_var:-} url=${!url_var:-} ref=${!ref_var:-}
    local dir="${REPOS_DIR}/${name}"

    [[ -d "${dir}" ]] || return 0

    # SRC_<NAME> is the compose-side truth; an empty directory alone could be a mounted checkout that happens to be
    # empty.
    if [[ -n "${src}" ]]; then
        REPO_FACTS+=("${name}|mounted|${src}")
        return 0
    fi
    fix_owner "${dir}"
    if [[ -e "${dir}/.git" ]]; then
        REPO_FACTS+=("${name}|cloned|${url}")
        return 0
    fi
    if [[ -z "${url}" ]]; then
        log "${name}: not mounted and no ${url_var}; skipping"
        return 0
    fi

    log "${name}: cloning ${url}${ref:+ @ ${ref}}"
    # ocudu keeps its history (reviews diff against a base), but without blobs until needed; the reference repos are
    # only read, so a shallow clone is enough.
    local depth=(--depth 1)
    [[ "${name}" == ocudu ]] && depth=(--filter=blob:none)
    git clone -q "${depth[@]}" ${ref:+--branch "${ref}"} "${url}" "${dir}"
    REPO_FACTS+=("${name}|cloned|${url}")
}

# Skills listed in skills.txt, as paths relative to $HOME, linked into ~/.claude/skills.
link_skills() {
    local skills="${HOME}/.claude/skills" entry src linked=() missing=()
    mkdir -p "${skills}"
    # ~/.claude is a persistent volume; drop the links an earlier start made.
    find "${skills}" -maxdepth 1 -type l -delete
    while read -r entry; do
        entry=${entry%%#*}
        entry=${entry//[[:space:]]/}
        [[ -n "${entry}" ]] || continue
        src="${HOME}/${entry}"
        if [[ -f "${src}/SKILL.md" ]]; then
            ln -sfn "${src}" "${skills}/$(basename "${entry}")"
            linked+=("$(basename "${entry}")")
        else
            missing+=("${entry}")
        fi
    done < "${CONF_DIR}/skills.txt"
    [[ ${#linked[@]} -eq 0 ]] || log "skills: ${linked[*]}"
    [[ ${#missing[@]} -eq 0 ]] || log "skills not found: ${missing[*]}"
}

write_manifest() {
    local entry name source origin
    {
        printf '# Generated on container start; edits are lost.\n'
        printf 'container: "%s"\n' "$(hostname)"
        printf 'project: "%s"\n' "${PROJECT_DIR}"
        printf 'results: "%s"          # persists, on the host\n' "${HOME}/results"
        printf 'workspace: "%s"   # writable, lost with the container\n' "${HOME}/work/ocudu"
        printf 'repos:\n'
        for entry in ${REPO_FACTS[@]+"${REPO_FACTS[@]}"}; do
            IFS='|' read -r name source origin <<<"${entry}"
            printf '  %s:\n    path: "%s/%s"\n    source: %s\n' \
                "${name}" "${REPOS_DIR}" "${name}" "${source}"
            printf '    writable: %s\n' "$([[ ${source} == mounted ]] && echo false || echo true)"
            [[ -z "${origin}" ]] || printf '    origin: "%s"\n' "${origin}"
        done
    } > "${MANIFEST}"
}

# The workspace is what every review edits, so it is made here rather than left as a step to remember. ~/work is
# container-local, so each start makes a fresh one. A failure is reported but does not stop the container: the shell is
# still useful to find out why.
init_workspace() {
    [[ -e "${REPOS_DIR}/ocudu/.git" ]] || {
        log "workspace: no ocudu repo; set SRC_OCUDU or CLONE_OCUDU"
        return 0
    }
    if review-workspace init ${REVIEW_REF:+--ref "${REVIEW_REF}"} >/dev/null 2>"${HOME}/.workspace-init.log"; then
        log "workspace: ~/work/ocudu at ${REVIEW_REF:-HEAD + uncommitted changes}"
    else
        log "workspace: init failed, see ~/.workspace-init.log"
        sed 's/^/    /' "${HOME}/.workspace-init.log" >&2
    fi
}

seed_claude_config
seed_git
for repo in ${BOX_REPOS}; do
    bootstrap_repo "${repo}"
done
write_manifest
link_skills
init_workspace
log "results: ~/results  ->  ${REVIEW_BOX_HOST_RESULTS:-<host results dir>}"
log "ready: run claude"

exec "$@"

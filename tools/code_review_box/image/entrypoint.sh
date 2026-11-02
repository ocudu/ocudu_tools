#!/usr/bin/env bash

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

# Prepares the review box on every start: Claude config, git, repos, skills.

set -euo pipefail

REPOS_DIR="${HOME}/repos"
WORKSPACE="${HOME}/work/ocudu"
CONF_DIR=/opt/review-box
MANIFEST="${HOME}/box.yml"
# Repos the box knows about. Each is mounted when MOUNTED_<NAME> is 1 (compose sets it from the host's SRC_<NAME>, whose
# path the box is not told), else cloned from CLONE_<NAME> (at REF_<NAME>) into the container and discarded when it
# exits.
BOX_REPOS="ocudu ocudu_docs 3gpp_md ocudu_tools"

REPO_FACTS=()

log() { printf '[review-box] %s\n' "$*" >&2; }

# Docker creates missing bind-mount parents as root; hand them back.
fix_owner() {
    [[ -e "$1" && ! -O "$1" ]] && sudo chown "$(id -u):$(id -g)" "$1" || true
}

# Claude authenticates with a token from the environment (CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY); nothing of the
# host's Claude config is mounted.
seed_claude_config() {
    fix_owner "${HOME}/.claude"
    [[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}${ANTHROPIC_API_KEY:-}" ]] ||
        log "warning: no CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY; claude will ask to log in"
    # Box rules and permissions are user-level, so they apply in the workspace on top of ocudu's own project files.
    # ~/.claude lives and dies with the container, so a review starts from nothing an earlier one left. The rules are
    # assembled from CLAUDE.<part>.md files, named so that Claude does not load them when editing the box's own
    # sources.
    install -m 644 "${CONF_DIR}/CLAUDE.general.md" "${HOME}/.claude/CLAUDE.md"
    install -m 644 "${CONF_DIR}/settings.json" "${HOME}/.claude/settings.json"
    install -m 644 "${CONF_DIR}/headless-settings.json" "${HOME}/.claude/headless-settings.json"
    [[ ${REVIEW_BOX_GLAB_COMMENT:-0} != 1 ]] || enable_glab_comment

    # ~/.claude.json is not in a volume, so each start writes it fresh: onboarding done, and the workspace trusted,
    # since it is a copy of the developer's own tree. An API key is pre-approved by its last 20 characters, as Claude
    # records it.
    jq -n --arg p "${WORKSPACE}" --arg key "${ANTHROPIC_API_KEY:-}" '
        {hasCompletedOnboarding: true, projects: {($p): {hasTrustDialogAccepted: true}}}
        + (if $key == "" then {} else {customApiKeyResponses: {approved: [$key[-20:]], rejected: []}} end)' \
        > "${HOME}/.claude.json"
    chmod 600 "${HOME}/.claude.json"
}

# Lets Claude post MR notes through review-post-note, which checks the prefix, and nothing else that writes: glab's own
# note commands stay denied. The token's scope was checked by review-box.sh.
enable_glab_comment() {
    local dir="${HOME}/.claude" tmp
    tmp=$(mktemp)
    jq '.permissions.deny -= ["Bash(review-post-note*)"]' \
        "${dir}/settings.json" > "${tmp}" && install -m 644 "${tmp}" "${dir}/settings.json"
    jq '.permissions.allow += ["Bash(review-post-note *)"]' \
        "${dir}/headless-settings.json" > "${tmp}" && install -m 644 "${tmp}" "${dir}/headless-settings.json"
    rm -f "${tmp}"
    cat "${CONF_DIR}/CLAUDE.glab-comment.md" >> "${dir}/CLAUDE.md"
    log "GitLab comments enabled: Claude may post MR notes"
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
    local mounted_var="MOUNTED_${key}" url_var="CLONE_${key}" ref_var="REF_${key}"
    local mounted=${!mounted_var:-} url=${!url_var:-} ref=${!ref_var:-}
    local dir="${REPOS_DIR}/${name}"

    [[ -d "${dir}" ]] || return 0

    # MOUNTED_<NAME> is the compose-side truth; an empty directory alone could be a mounted checkout that happens to
    # be empty.
    if [[ "${mounted}" == 1 ]]; then
        REPO_FACTS+=("${name}|mounted|")
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
        printf 'results: "%s"          # persists, on the host\n' "${HOME}/results"
        printf 'workspace: "%s"   # writable, lost with the container\n' "${WORKSPACE}"
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
        log "workspace: no ocudu repo; set SRC_OCUDU or CLONE_OCUDU in .env"
        return 0
    }
    local at=${REVIEW_REF:-HEAD + uncommitted changes}
    [[ -z "${REVIEW_MR:-}" ]] || at="MR !${REVIEW_MR}"
    if review-workspace init ${REVIEW_REF:+--ref "${REVIEW_REF}"} ${REVIEW_MR:+--mr "${REVIEW_MR}"} \
        >/dev/null 2>"${HOME}/.workspace-init.log"; then
        log "workspace: ~/work/ocudu at ${at}"
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
log "results: ~/results, kept on the host (review-box.sh printed where)"
log "ready: run claude"

# Claude starts in the ocudu workspace, so ocudu's own project instructions load.
[[ -d "${WORKSPACE}" ]] && cd "${WORKSPACE}"
exec "$@"

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

# AI box, base target: Claude Code plus ocudu_tools' skills and the box's scripts. No OCUDU toolchain.
# Build context is the ocudu_tools root (see Dockerfile.dockerignore).

FROM python:3.12-slim AS base

ARG HOST_UID=1000
ARG HOST_GID=1000
ARG CLAUDE_CODE_VERSION=2.1.274

ENV LANG=C.UTF-8
LABEL io.srs.ci-box="true"

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl git jq \
    && rm -rf /var/lib/apt/lists/*

# Host uid/gid, so results written to a bind mount belong to the developer.
RUN (getent group "${HOST_GID}" || groupadd -g "${HOST_GID}" ai) \
    && useradd -m -u "${HOST_UID}" -g "${HOST_GID}" -s /bin/bash ai

USER ai
ENV HOME=/home/ai
ENV PATH="/opt/ocudu_tools/tools/ci_box/bin:${HOME}/.local/bin:${PATH}"

RUN curl -fsSL https://claude.ai/install.sh | bash -s "${CLAUDE_CODE_VERSION}" \
    && rm -rf "${HOME}/.claude" "${HOME}/.claude.json" \
    && claude --version

# Last, so editing a skill only rebuilds these layers.
COPY skills /opt/ocudu_tools/skills
COPY tools/ci_box /opt/ocudu_tools/tools/ci_box
RUN mkdir -p "${HOME}/.claude/skills" "${HOME}/results" \
    && for d in /opt/ocudu_tools/skills/*/; do ln -s "${d%/}" "${HOME}/.claude/skills/"; done

WORKDIR /home/ai
CMD ["bash", "-l"]

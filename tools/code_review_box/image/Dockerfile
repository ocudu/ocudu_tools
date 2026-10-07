# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

# OCUDU code review box: everything a Claude session needs to review OCUDU code locally.
#
# One image on purpose. The review needs the OCUDU build toolchain (to configure the tree and get a
# compile_commands.json), the formatters and checkers the OCUDU CI runs, and include-what-you-use. Repos are not baked
# in: they are mounted or cloned on start.

ARG OS_VERSION=26.04
FROM ubuntu:${OS_VERSION}

ARG USERNAME=dev
ARG HOST_UID=1000
ARG HOST_GID=1000
ARG GLAB_VERSION=1.113.0
# Seeds a fresh CLI volume only; from there the autoupdater takes over.
ARG CLAUDE_CODE_VERSION=latest

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

# review-box.sh finds and prunes images by this label.
LABEL io.srs.review-box="true"

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg sudo locales \
        git openssh-client \
        python3 python3-pip python3-venv \
        ripgrep fd-find jq less vim file \
        build-essential cmake ninja-build ccache pkg-config gdb \
    && rm -rf /var/lib/apt/lists/*

# OCUDU's build dependencies: the "build" set of its docker/scripts/install_dependencies.sh.
# The "extra" RF set (uhd, dpdk, zmq, capnproto) is left out; a review never runs radios.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libfftw3-dev libmbedtls-dev libsctp-dev libyaml-cpp-dev libgtest-dev \
    && rm -rf /var/lib/apt/lists/*

# Review tooling.
#   clang-format   the version OCUDU's apply-format and CI call by name (clang-format-21).
#   clang-tidy     static checks a deep review can lean on.
#   iwyu           include-what-you-use and fix_include, for include hygiene.
#   clang, llvm    iwyu is clang-based and reads a clang-compatible compile_commands.json.
RUN apt-get update && apt-get install -y --no-install-recommends \
        clang clang-format clang-tidy clang-tools llvm iwyu \
    && rm -rf /var/lib/apt/lists/* \
    && update-ccache-symlinks

# glab reads MR metadata (title, target branch, discussions) when reviewing an MR.
RUN curl -fsSL -o /tmp/glab.deb \
        "https://gitlab.com/gitlab-org/cli/-/releases/v${GLAB_VERSION}/downloads/glab_${GLAB_VERSION}_linux_amd64.deb" \
    && dpkg -i /tmp/glab.deb && rm /tmp/glab.deb

# Ubuntu marks its python install externally managed, so python tools get a venv.
#   reuse    drives OCUDU's annotate_headers.sh (SPDX copyright headers).
#   pyyaml   ocudu-code-review's dependency checker.
ENV VIRTUAL_ENV=/opt/venv
ENV PATH=/opt/venv/bin:${PATH}
RUN python3 -m venv ${VIRTUAL_ENV} \
    && ${VIRTUAL_ENV}/bin/pip install --no-cache-dir reuse pyyaml tabulate

# Fail the build, not the first review, if a tool the checks call by name is missing.
RUN command -v clang-format-21 >/dev/null \
    && command -v include-what-you-use >/dev/null \
    && reuse --version >/dev/null

# Ubuntu images ship a uid-1000 "ubuntu" user that collides with the host uid.
RUN if getent passwd ${HOST_UID} >/dev/null; then \
        userdel -r "$(getent passwd ${HOST_UID} | cut -d: -f1)" 2>/dev/null || true; \
    fi \
    && groupadd -f -g ${HOST_GID} ${USERNAME} \
    && useradd -m -u ${HOST_UID} -g ${HOST_GID} -s /bin/bash ${USERNAME} \
    && echo "${USERNAME} ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/${USERNAME} \
    && chmod 0440 /etc/sudoers.d/${USERNAME} \
    && chown -R ${USERNAME}:${USERNAME} ${VIRTUAL_ENV}

COPY bin/ /usr/local/bin/
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/review-* /usr/local/bin/git-remote-push-disabled /usr/local/bin/entrypoint.sh

RUN mkdir -p /opt/review-box
COPY skills.txt /opt/review-box/

USER ${USERNAME}
ENV HOME=/home/${USERNAME}
# ccache shims first, so builds pick up the cache shared with the host without knowing it.
ENV PATH="/usr/lib/ccache:${PATH}:${HOME}/.local/bin"

# Mount points, created as this user so volumes and bind mounts land with the right owner.
#   repos/    ocudu (read-only), ocudu_docs, 3gpp_md, ocudu_tools
#   box/      this box's own directory (skills), read-only
#   review/   the project Claude starts in
#   results/  this container's reports and patches, on the host
#   work/     the writable review workspace and build tree, inside the container only
RUN mkdir -p ${HOME}/repos ${HOME}/review ${HOME}/results ${HOME}/work \
        ${HOME}/.claude ${HOME}/.cache/ccache

# Native install, as this user, so the autoupdater can replace it without root.
RUN curl -fsSL https://claude.ai/install.sh | bash -s "${CLAUDE_CODE_VERSION}" \
    && rm -rf ${HOME}/.claude/* ${HOME}/.claude.json \
    && test -x ${HOME}/.local/bin/claude

WORKDIR ${HOME}/review
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["bash", "-l"]

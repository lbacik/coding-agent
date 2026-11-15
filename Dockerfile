# syntax=docker/dockerfile:1

# coding-agent base image (the default output of `docker build .`).
#
# This image packages coding-agent and agent-installer plus only the
# dependencies those applications need (Python/uv for the Worker, Node for
# agent-installer). It is NOT a runnable Worker deployment: it carries no
# Target Project toolchain (PHP/Composer/pnpm), no Skill Bundle, no
# Workflow Configuration or prompts, and no Supported Toolchain Matrix.
# `agent run` against it refuses at startup, naming the missing artifact.
#
# A runnable deployment image is assembled FROM this base by provisioning
# the Target Project's toolchains, generating the Supported Toolchain
# Matrix from an explicit runtime selection, and installing the Skill
# Bundle. The `deployment` stage below is the test/reference example of
# such a derived image (build it with `--target deployment`); production
# deployments build their own derived image the same way. The default
# (last) stage stays the base image, so a plain `docker build .` never
# produces a deployment.
#
# Downstream: Agent Forge currently builds this repository's Dockerfile
# directly. Its adapter must be updated to build a derived deployment
# image before the new base can serve as a complete Worker deployment.

# Supported Toolchain Matrix (see CONTEXT.md), pinned per ADR 0001 and the
# #6 resolution's Project Profile examples. The PHP/Composer/pnpm pins live
# on the `deployment` stage: they are Target Project toolchains, not base
# image dependencies.
ARG PYTHON_IMAGE=python:3.13-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e
ARG NODE_MAJOR=22
ARG NODE_VERSION=22.23.2-1nodesource1
ARG UV_VERSION=0.5.29

FROM ${PYTHON_IMAGE} AS base

ARG NODE_MAJOR
ARG NODE_VERSION
ARG UV_VERSION

# uv must never provision a Python interpreter during an Attempt (ADR 0001):
# a version outside the matrix is an Unsupported Environment, not a download.
ENV UV_PYTHON_DOWNLOADS=never \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      gnupg \
      git \
      unzip \
    && rm -rf /var/lib/apt/lists/*

# Node, from NodeSource, pinned to one package version: agent-installer
# runs on Node, so a compatible runtime is a base-image dependency. It is
# not a Target Project toolchain: pnpm (and any Node project toolchain)
# is provisioned by derived images only.
RUN curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs=${NODE_VERSION} \
    && rm -rf /var/lib/apt/lists/*

# uv, pinned to one version, installed as a Python package alongside the
# interpreter it drives.
RUN pip install --no-cache-dir "uv==${UV_VERSION}"

RUN useradd --create-home --home-dir /home/agent --uid 1000 --shell /bin/bash agent

RUN install -d -o agent -g agent /var/lib/coding-agent \
    /var/lib/coding-agent/mirrors \
    /var/lib/coding-agent/workspaces \
    /var/lib/coding-agent/caches \
    /var/lib/coding-agent/caches/uv \
    /var/lib/coding-agent/artifacts \
    /var/lib/coding-agent/logs

VOLUME ["/var/lib/coding-agent"]

# agent-installer, declared as a `>=0.8.0` range in
# docker/agent-installer/package.json with the exact resolved release pinned
# by this repository's own lockfile (`npm ci` stays reproducible; an upgrade
# is a lockfile refresh, not a code change) rather than a global install:
# the installer declares its own runtime dependencies with caret ranges and
# publishes no shrinkwrap, so pinning the installer's own version would not
# pin its dependency tree. 0.8.0 is the minimum release satisfying
# agent-installer#74 (complete Git artifact provenance, freshly verified
# installed copies, offline subset verification); 0.6.0 silently dropped
# `install --ref` (the root program swallowed the option), so it installed
# upstream HEAD whatever SKILLS_REF said; 0.7.x honoured it but lacked the
# complete provenance the Skill Bundle deployment requires.
COPY docker/agent-installer/package.json docker/agent-installer/package-lock.json /opt/agent-installer/
RUN cd /opt/agent-installer && npm ci
ENV PATH="/opt/agent-installer/node_modules/.bin:${PATH}"

# The three verification layers are this project's responsibility, not
# agent-installer's (#6 §5, issue #15): comparing what got installed against
# the Skill Bundle, dependency closure, and "is this bundle complete".
# Installed here, ahead of the runtime user switch, so uv's own
# lockfile-driven install matches the toolchain above.
WORKDIR /opt/coding-agent
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev
ENV PATH="/opt/coding-agent/.venv/bin:${PATH}"
# Operator contract CLI (issue #247, adoption step 4): the contract CLI is
# served from /usr/local/bin/agentctl so `docker exec --user agent` reaches
# it on the read-only root filesystem without a TTY. Installed here, ahead
# of the runtime user switch, as a symlink into the lockfile-driven venv.
RUN ln -s /opt/coding-agent/.venv/bin/agentctl /usr/local/bin/agentctl
# A container's stdout is a pipe, not a TTY, so Python would block-buffer
# it: `docker compose up worker` then showed no Terminal Outcome line until
# the buffer filled (seen on the first real delivery, #154).
ENV PYTHONUNBUFFERED=1

# Redirects the cache and writable state the base image's own tools write
# off the read-only root filesystem and onto the volume layout above. Set
# only now, after every root-owned build step above: setting it earlier
# would have this build's own `uv sync` calls write root-owned cache
# entries onto /var/lib/coding-agent, which Docker then bakes into a fresh
# named volume's initial content — unwritable by the agent user at runtime.
# uv follows XDG_CACHE_HOME natively. Composer and pnpm need their own
# redirects, set on the `deployment` stage that installs them.
ENV XDG_CACHE_HOME=/var/lib/coding-agent/caches

USER agent
ENV HOME=/home/agent
WORKDIR /home/agent

# The image's default command is a useful CLI default. It presumes no
# Workflow Configuration and selects no application scenario: a runnable
# deployment is a derived image plus an operator-supplied command such as
# `agent run --config /etc/coding-agent/workflow.yml --workflow implement`.
# There is still no entrypoint, so every other command — the verification
# scripts' `bash`, `id`, `agent validate` — is simply passed in its place.
CMD ["agent", "--help"]

# --- The reference deployment image (test-only) ---
#
# Built with `docker build --target deployment .`. It derives from the base
# image without changing it: the existing pinned test toolchains
# (Python/uv, PHP 8.4 with extensions + Composer, Node + pnpm), the
# Supported Toolchain Matrix generated from an explicit runtime selection,
# and the baked v3 instruction package (`docker/deployment/`: a `version: 3`
# Workflow Configuration, its prompt files, and the reviewed
# skill-policy.json) with the Skill Bundle prepared from it at build time.
# The build runs, as the runtime `agent` user and with no Worker
# GitHub/model secrets set: `agent config validate` (secret-free, SKV3-20),
# `agent prepare-skill-bundle --mode install`, then `agent
# verify-skill-bundle` against the final roots after the last relevant copy
# (SKV3-33). An edited copy or a missing declared member fails the build.
# Compose's `worker` and `validate` services build this target as
# the reference/example deployment image, not as a production image.

FROM base AS deployment

ARG PHP_MINOR=8.4
ARG PNPM_VERSION=9.15.4
ARG COMPOSER_VERSION=2.8.9
# No SKILLS_* build args and no secret build args: the Skill Bundle
# membership comes from the baked `docker/deployment/workflow.yml`, and the
# build runs with no Worker GitHub/model secrets set (SKV3-20, image).

USER root

# PHP, from Sury (Debian bookworm has no 8.4 in its own repos), pinned to one
# minor version via the package names (php<minor>-*), with the extension set
# common Composer projects need. There is deliberately no exact patch pin:
# sury builds each architecture separately, publishes a different version
# string per arch, and drops superseded builds, so an exact `=<version>` pin
# cannot stay correct on both amd64 and arm64. Any patch build sury currently
# offers for the pinned minor is acceptable; apt resolves the whole set from
# one index update, and the exact patch baked into the image is recorded as
# a build output in /opt/coding-agent/toolchain-matrix.json. To build another
# minor, pass --build-arg PHP_MINOR=<major>.<minor> (e.g. 8.4).
RUN curl -fsSL https://packages.sury.org/php/apt.gpg -o /usr/share/keyrings/sury-php.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/sury-php.gpg] https://packages.sury.org/php/ bookworm main" \
         > /etc/apt/sources.list.d/sury-php.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
         php${PHP_MINOR}-cli \
         php${PHP_MINOR}-common \
         php${PHP_MINOR}-mbstring \
         php${PHP_MINOR}-xml \
         php${PHP_MINOR}-curl \
         php${PHP_MINOR}-zip \
         php${PHP_MINOR}-intl \
         php${PHP_MINOR}-bcmath \
         php${PHP_MINOR}-sqlite3 \
         php${PHP_MINOR}-mysql \
         php${PHP_MINOR}-pgsql \
    && rm -rf /var/lib/apt/lists/*

# Composer, pinned to one version, verified against its published installer
# signature before it is trusted to run.
RUN curl -fsSL https://getcomposer.org/installer -o /tmp/composer-setup.php \
    && EXPECTED_SIGNATURE="$(curl -fsSL https://composer.github.io/installer.sig)" \
    && ACTUAL_SIGNATURE="$(php -r "echo hash_file('sha384', '/tmp/composer-setup.php');")" \
    && [ "$EXPECTED_SIGNATURE" = "$ACTUAL_SIGNATURE" ] \
    && php /tmp/composer-setup.php --version=${COMPOSER_VERSION} --install-dir=/usr/local/bin --filename=composer \
    && rm /tmp/composer-setup.php

# pnpm, pinned to one version. Not Corepack: since Node 22's bundled Corepack
# (>=0.31), `prepare --activate` installs a shim that still resolves and
# downloads whatever version the registry calls latest at invocation time
# when no project-local `packageManager` field is present — the same
# in-Attempt provisioning ADR 0001 forbids for Python, silently, for pnpm.
RUN npm install -g "pnpm@${PNPM_VERSION}" --no-fund --no-audit

# The deployment's package-manager cache and writable state, redirected onto
# the volume like the base image's own uv cache: Composer's cache follows
# XDG_CACHE_HOME too, but its config home (config.json/auth.json) does not —
# COMPOSER_HOME covers both in one setting, verified against
# `composer config --global home` under a read-only rootfs. pnpm needs both
# its store-dir (its own config env var) and its update-check state
# (XDG_STATE_HOME) named explicitly.
RUN install -d -o agent -g agent /var/lib/coding-agent/caches/composer \
    /var/lib/coding-agent/caches/pnpm
ENV COMPOSER_HOME=/var/lib/coding-agent/caches/composer
ENV XDG_STATE_HOME=/var/lib/coding-agent/caches/pnpm
ENV npm_config_store_dir=/var/lib/coding-agent/caches/pnpm

# The Supported Toolchain Matrix: a build output read by later slices'
# toolchain assertion, not only by a human. Generated here, after this
# stage provisioned the deployment's actual toolchains, from an explicit
# runtime selection — never inferred from whatever is on PATH, since the
# base image's own Python+uv (and Node) are not Target Project toolchains.
COPY scripts/generate-toolchain-matrix.py /opt/coding-agent/generate-toolchain-matrix.py
RUN python3 /opt/coding-agent/generate-toolchain-matrix.py \
      --runtime python --runtime php --runtime node \
      --output /opt/coding-agent/toolchain-matrix.json

# --- The baked v3 instruction package (reference deployment) ---
#
# The baked roots: the `version: 3` Workflow Configuration, the prompt files
# it names (resolved relative to the configuration, so they stay beside it),
# and the reviewed skill-policy.json. An operator may instead mount an
# external prepared package and pass its explicit paths to `agent run` (see
# compose.yaml); the baked files stay the image default.
COPY docker/deployment/workflow.yml /etc/coding-agent/workflow.yml
COPY docker/deployment/prompts /etc/coding-agent/prompts
COPY docker/deployment/skill-policy.json /etc/coding-agent/skill-policy.json

# The prepared bundle directory, owned by the runtime user ahead of the
# user switch so build-time preparation writes no root-owned bytes into it.
RUN install -d -o agent -g agent /opt/coding-agent/skill-bundle

USER agent

# Build-time preparation, never at startup: the image tag is the Skill
# Bundle's version. Runs as the runtime `agent` user with no Worker
# GitHub/model secrets set. `config validate` is static and offline; `mode
# install` resolves and installs the declared subset through the
# installer-adapter port (network only here); the final `verify-skill-bundle`
# re-checks the final roots after the last relevant copy, so it — not any
# builder-stage log — is the acceptance proof (SKV3-33). No COPY of the
# baked roots or the bundle directory may follow this step. A missing
# declared member fails the build here, naming what was wrong.
RUN agent config validate --config /etc/coding-agent/workflow.yml \
    && agent prepare-skill-bundle --mode install \
      --config /etc/coding-agent/workflow.yml \
      --policy /etc/coding-agent/skill-policy.json \
      --bundle-dir /opt/coding-agent/skill-bundle \
      --home /home/agent \
    && agent verify-skill-bundle \
      --config /etc/coding-agent/workflow.yml \
      --policy /etc/coding-agent/skill-policy.json \
      --bundle-dir /opt/coding-agent/skill-bundle \
      --home /home/agent

# The default (last) stage stays the base image: a plain `docker build .`
# produces the reusable base, never this deployment. `docker build
# --target deployment .` selects the stage above.
FROM base

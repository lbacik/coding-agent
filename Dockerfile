# syntax=docker/dockerfile:1

# Supported Toolchain Matrix (see CONTEXT.md), pinned per ADR 0001 and the
# #6 resolution's Project Profile examples.
ARG PYTHON_IMAGE=python:3.13-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e
ARG NODE_MAJOR=22
ARG NODE_VERSION=22.23.2-1nodesource1
ARG PHP_MINOR=8.4
ARG PHP_VERSION=8.4.25-1+0~20260828.55+debian12~1.gbp259445
ARG PNPM_VERSION=9.15.4
ARG UV_VERSION=0.5.29
ARG COMPOSER_VERSION=2.8.9

FROM ${PYTHON_IMAGE}

ARG NODE_MAJOR
ARG NODE_VERSION
ARG PHP_MINOR
ARG PHP_VERSION
ARG PNPM_VERSION
ARG UV_VERSION
ARG COMPOSER_VERSION

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

# Node, from NodeSource, pinned to one package version.
RUN curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs=${NODE_VERSION} \
    && rm -rf /var/lib/apt/lists/*

# PHP, from Sury (Debian bookworm has no 8.4 in its own repos), pinned to one
# package version, with the extension set common Composer projects need.
RUN curl -fsSL https://packages.sury.org/php/apt.gpg -o /usr/share/keyrings/sury-php.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/sury-php.gpg] https://packages.sury.org/php/ bookworm main" \
         > /etc/apt/sources.list.d/sury-php.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
         php${PHP_MINOR}-cli=${PHP_VERSION} \
         php${PHP_MINOR}-common=${PHP_VERSION} \
         php${PHP_MINOR}-mbstring=${PHP_VERSION} \
         php${PHP_MINOR}-xml=${PHP_VERSION} \
         php${PHP_MINOR}-curl=${PHP_VERSION} \
         php${PHP_MINOR}-zip=${PHP_VERSION} \
         php${PHP_MINOR}-intl=${PHP_VERSION} \
         php${PHP_MINOR}-bcmath=${PHP_VERSION} \
         php${PHP_MINOR}-sqlite3=${PHP_VERSION} \
         php${PHP_MINOR}-mysql=${PHP_VERSION} \
         php${PHP_MINOR}-pgsql=${PHP_VERSION} \
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

# uv, pinned to one version, installed as a Python package alongside the
# interpreter it drives.
RUN pip install --no-cache-dir "uv==${UV_VERSION}"

RUN useradd --create-home --home-dir /home/agent --uid 1000 --shell /bin/bash agent

RUN install -d -o agent -g agent /var/lib/coding-agent \
    /var/lib/coding-agent/mirrors \
    /var/lib/coding-agent/workspaces \
    /var/lib/coding-agent/caches \
    /var/lib/coding-agent/caches/uv \
    /var/lib/coding-agent/caches/composer \
    /var/lib/coding-agent/caches/pnpm \
    /var/lib/coding-agent/artifacts \
    /var/lib/coding-agent/logs

VOLUME ["/var/lib/coding-agent"]

# The Supported Toolchain Matrix: a build output read by later slices'
# toolchain assertion, not only by a human. Generated here, from the
# toolchains this build actually produced, rather than hand-copied from the
# pins above.
RUN install -d /opt/coding-agent \
    && python3 - <<'PY'
import json
import subprocess


def run(*cmd: str) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()


matrix = {
    "schema": 1,
    "toolchains": {
        "python": {
            "version": run("python3", "--version").removeprefix("Python "),
            "package_manager": {"name": "uv", "version": run("uv", "--version").split()[1]},
        },
        "php": {
            "version": run("php", "-r", "echo PHP_VERSION;"),
            "package_manager": {"name": "composer", "version": run("composer", "--version").split()[2]},
        },
        "node": {
            "version": run("node", "--version").removeprefix("v"),
            "package_manager": {"name": "pnpm", "version": run("pnpm", "--version")},
        },
    },
}

with open("/opt/coding-agent/toolchain-matrix.json", "w") as f:
    json.dump(matrix, f, indent=2, sort_keys=True)
    f.write("\n")
PY

# Redirects each package manager's cache and writable state off the
# read-only root filesystem and onto the volume layout above, without which
# a first real bootstrap would try to write under $HOME and fail. uv follows
# XDG_CACHE_HOME natively. Composer's cache follows it too, but its config
# home (config.json/auth.json) does not — COMPOSER_HOME covers both in one
# setting, verified against `composer config --global home` under a
# read-only rootfs. pnpm needs both its store-dir (its own config env var)
# and its update-check state (XDG_STATE_HOME) named explicitly.
ENV XDG_CACHE_HOME=/var/lib/coding-agent/caches
ENV COMPOSER_HOME=/var/lib/coding-agent/caches/composer
ENV XDG_STATE_HOME=/var/lib/coding-agent/caches/pnpm
ENV npm_config_store_dir=/var/lib/coding-agent/caches/pnpm

USER agent
ENV HOME=/home/agent
WORKDIR /home/agent

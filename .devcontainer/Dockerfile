# The development environment of rcman's own repository, for a workspace of rcman or any tool
# that reads devcontainer.json: what make setup and every target of the Makefile need, Podman for
# the tests' engine and Caddy for the whole stack (project-knowledge/tests.md), and a user dev who
# is not root and has no way to become root. uv and Node.js are the installer's pins
# (installer/install.py), the Playwright whose Chromium's packages are installed is
# frontend/package-lock.json's, and the base is the default workspace image's;
# tests/test_repository_devcontainer.py holds them to those.
FROM docker.io/library/ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55
ARG UV_VERSION=0.12.21
ARG UV_SHA256_AMD64=23f02075b652bb1df64178cfae41b5caf160822e720e2663568f3f5d63bc52c0
ARG UV_SHA256_ARM64=030b69227b40af8c1981b7301793dc66e71ed3c796ea8688209dd268bd91ec51
ARG NODE_VERSION=24.21.0
ARG NODE_SHA256_AMD64=fd8e59d5a511510f6a298afb548f18c7d2b1be404d8b4a27d94fbe49f56cb2d6
ARG NODE_SHA256_ARM64=6ad1325edbdb5649c379b75a237147a666c95d4f9ae8d340fef2d1575d289ad2
ARG PLAYWRIGHT_VERSION=1.63.0
RUN apt-get update -qq \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      podman crun catatonit uidmap make git gh tmux busybox-static curl xz-utils python3 caddy \
      ca-certificates libcap2-bin \
 && rm -rf /var/lib/apt/lists/*
# uv and Node.js, each checked against its pinned sha256 before it is unpacked.
RUN case "$(dpkg --print-architecture)" in \
      amd64) uv=x86_64 node=x64 uv_sum=$UV_SHA256_AMD64 node_sum=$NODE_SHA256_AMD64 ;; \
      arm64) uv=aarch64 node=arm64 uv_sum=$UV_SHA256_ARM64 node_sum=$NODE_SHA256_ARM64 ;; \
      *) echo "no pinned uv or Node.js for this machine" >&2; exit 1 ;; \
    esac \
 && curl -fsSL -o /tmp/uv.tar.gz \
      "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$uv-unknown-linux-gnu.tar.gz" \
 && echo "$uv_sum  /tmp/uv.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/uv.tar.gz -C /usr/local/bin --strip-components=1 \
 && curl -fsSL -o /tmp/node.tar.xz \
      "https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-linux-$node.tar.xz" \
 && echo "$node_sum  /tmp/node.tar.xz" | sha256sum -c - \
 && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 \
 && rm /tmp/uv.tar.gz /tmp/node.tar.xz
# What Playwright's Chromium needs of the system; make setup installs the browser itself.
RUN apt-get update -qq \
 && npx --yes "playwright@$PLAYWRIGHT_VERSION" install-deps chromium \
 && rm -rf /var/lib/apt/lists/* /root/.npm
# newuidmap and newgidmap get the one capability each needs in place of setuid, as the default
# workspace image's do: a workspace has no CAP_SYS_ADMIN, which a setuid newuidmap would need.
RUN chmod u-s /usr/bin/newuidmap /usr/bin/newgidmap \
 && setcap cap_setuid+ep /usr/bin/newuidmap \
 && setcap cap_setgid+ep /usr/bin/newgidmap
# dev, with every id of the workspace but root's and its own as subordinate ids, for rootless
# Podman. The base image's own first user is removed for it.
RUN userdel --remove ubuntu \
 && useradd --create-home --uid 1000 --shell /bin/bash dev \
 && printf 'dev:1:999\ndev:1001:64535\n' > /etc/subuid \
 && cp /etc/subuid /etc/subgid
USER dev

# A throwaway Ubuntu for testing as root: user management with real useradd,
# userdel, runuser, sudo and tmux, and installing rcman with git, Caddy and a
# stand-in for systemctl. The repository is mounted read-only at /src.
FROM ubuntu:24.04
RUN apt-get update -qq \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      python3 python3-pytest tmux procps sudo passwd git curl ca-certificates xz-utils caddy \
 && rm -rf /var/lib/apt/lists/*
COPY fake-systemctl /usr/local/bin/systemctl

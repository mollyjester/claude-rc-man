# A throwaway Ubuntu for testing user management as root, with real useradd,
# userdel, runuser, sudo and tmux. The repository is mounted read-only at /src.
FROM ubuntu:24.04
RUN apt-get update -qq \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      python3 python3-pytest tmux procps sudo passwd \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd rcusers && useradd --system --user-group rcman

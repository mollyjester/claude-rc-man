# The podman host: a throwaway Ubuntu that runs systemd as PID 1, for rootless Podman
# under a service account, set up either by tests/container/podman_host.py or by the
# real installer. It is run privileged, with a cgroup namespace of its own. A snapshot
# of the checkout's files is mounted read-only at /src (tests/snapshot.py). It holds
# what the installer installs or needs already, so that it installs no package: git,
# Caddy, busybox-static, xz-utils, curl and ca-certificates; and pytest.
FROM ubuntu:24.04
RUN apt-get update -qq \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      systemd systemd-sysv dbus dbus-user-session podman uidmap catatonit slirp4netns \
      busybox-static ncurses-base python3 python3-pytest git caddy curl ca-certificates \
      xz-utils procps \
 && rm -rf /var/lib/apt/lists/*
# Root's own Podman is not used; its units would keep its storage on the image's overlay.
# Caddy is started by the tests that need it, with their own configuration.
RUN systemctl disable podman.service podman.socket podman-restart.service \
      podman-auto-update.timer podman-auto-update.service podman-clean-transient.service \
      caddy.service
STOPSIGNAL SIGRTMIN+3
# A cgroup that holds a process cannot enable controllers for its children, and docker
# exec can put one in the container's root cgroup while systemd is booting, which would
# leave systemd without the controllers it delegates. So PID 1 moves to a child cgroup
# and enables every controller at the root before systemd starts.
ENTRYPOINT ["/bin/sh", "-c", "mkdir /sys/fs/cgroup/init.scope && echo $$ > /sys/fs/cgroup/init.scope/cgroup.procs && sed 's/[^ ][^ ]*/+&/g' /sys/fs/cgroup/cgroup.controllers > /sys/fs/cgroup/cgroup.subtree_control && exec /sbin/init"]

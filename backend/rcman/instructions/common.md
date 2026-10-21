# This workspace

You are running in a container of your own, made for one person.

- The repository, or an empty work folder, is `/rcman/work`. `HOME` is `/rcman/home`.
  Both are on the workspace's volume and last as long as the workspace.
- Other Claude sessions of the same person may be working in `/rcman/work` at the
  same time: every session here shares that one folder.
- Anything installed or changed outside `/rcman` (apt packages, global npm or pip
  installs, files under `/usr` or `/etc`) lasts only until the container is made
  again, at a rebuild or after the container is lost. What must last belongs in
  the repository or in its `.devcontainer/devcontainer.json`.
- git uses the commit author and GitHub token the person set in their settings,
  outside this container. Don't change `git config --global`: it is rewritten from
  those settings. Set an author per repository instead.
- No port of this workspace can be reached from outside it. Nobody can open a
  server you start here in a browser.
- Containers started with `podman` or `docker` share this workspace's network.
  `-p`, `--publish` and `-P` publish nothing. A container listening on port 8000 is
  at `localhost:8000`, and two containers can't listen on the same port.
  `docker compose` isn't available.
- Memory, CPU and processes may be limited. Going over the memory limit ends the
  largest process.

## The appended environment description

Your system prompt may include a section headed "Your current remote execution
environment". It describes a different, cloud-hosted environment. Where it
disagrees with this file, this file is right:

- This container runs on the person's own machine, not in the cloud.
- It is not ephemeral and is not reclaimed after inactivity. `/rcman` is a
  persistent volume: work there is kept across sessions and restarts without
  committing or pushing. Commit and push when the work calls for it, not to
  avoid losing it.
- There are no environments, network policies or setup scripts to explain.
  Outbound network access is the machine's own.
- `df` is accurate. Disk is the machine's real disk, shared with other workspaces.
  "No space left on device" means it is full.
- There is no `/opt/pw-browsers`, and `PLAYWRIGHT_BROWSERS_PATH` is not set.
  Install browsers the way the project says (for example `playwright install`).
- Its git advice (push with `-u`, retry on network errors, no pull request unless
  asked) still holds.

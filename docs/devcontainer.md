# devcontainer.json support

A repository can say, in a devcontainer.json, which image its workspace should run in, or
how to build one with a Dockerfile and features, and what to run before the workspace is
ready. When a workspace is made by cloning a repository (see [Workspaces](workspaces.md)),
rcman reads that file, holds it to the policy below, pulls the image it names or builds the
one it describes ([Building an image](#building-an-image)), and makes the workspace's
container from that image instead of from the default one
([`RCMAN_WORKSPACE_IMAGE`](configuration.md)). A repository without the file gets the
default image. A workspace made empty never has a file to read.

This page is for administrators: what is supported, what is not, and what follows from it.
A person sees only the steps and, when something fails, the reason (see
[Workspaces](workspaces.md)).

## Where the file is looked for

rcman looks for `.devcontainer/devcontainer.json`, else `.devcontainer.json` in the
repository's top folder, and nowhere else: not in a subfolder, and not for a file
`.devcontainer/<folder>/devcontainer.json`. The file is read after the clone, from the
workspace, as it is on the branch that was checked out. It must be an ordinary file: a
symbolic link, or a file in a folder that is one, is refused, so that a repository cannot
make rcman read a file of the workspace's that is not its own. A file that is too big
(`MAX_BYTES` in `backend/rcman/devcontainer.py`) is refused unread. The file may hold
comments and trailing commas, as devcontainer.json does. Its content is never shown in a
message; a syntax error is reported by its line and column, and a key by its name.

A file that builds from a Dockerfile has that Dockerfile read too, the same way, so that a
change to it is seen (see [Rebuilding a workspace](#rebuilding-a-workspace)): a Dockerfile
that is a symbolic link, or is in a folder that is one, is refused, and one bigger than the
same `MAX_BYTES` is refused unread.

## What each key does

The tables below are the policy itself, key by key; a test holds them equal to it.

- **Honoured** keys are applied.
- **Ignored** keys are skipped, and the workspace's steps say which, by name.
- **Refused** keys fail the workspace, and its reason names the key. A key that asks for
  nothing (false, null, an empty list, object or string) is not refused. A dotted key is a
  key inside another key's object, and a key shown with `=false` is refused only when set
  to false.
- **Any other key is refused** as unknown, with its name, since rcman cannot say what it
  would do.

The same policy holds for the settings an image carries in its `devcontainer.metadata`
label: a refused or unknown key in it fails the workspace, naming the key and the entry. An
entry of a label may not carry the keys that describe a build (`build`, `image`, `features`,
`dockerFile`, `context`, `overrideFeatureInstallOrder`) either.

### Honoured

| Key | What rcman does | Why |
|---|---|---|
| `image` | honoured | The image is pulled by rcman, and the workspace's container is made from it. With features it is the image they are installed on, and the container is made from the result. |
| `remoteUser` | honoured | The user rcman's commands and Claude run as. It must be in the image, and it is mapped onto the same host user as root has in your other workspaces. |
| `containerUser` | honoured | Used as remoteUser when that is not set. |
| `updateRemoteUserUID` | honoured | Accepted. rcman always maps the user onto the same host user as root, so no file needs a new owner. |
| `containerEnv` | honoured | Set for the container and for every command rcman runs in the workspace, Claude included. |
| `remoteEnv` | honoured | Set for every command rcman runs in the workspace, Claude included; a null value removes a variable. |
| `init` | honoured | Accepted. Every workspace's container runs with an init process. |
| `waitFor` | honoured | Accepted. The workspace is ready only after all its create commands have run. |
| `overrideCommand` | honoured | Accepted when absent or true. rcman runs its own process in the container. |
| `onCreateCommand` | honoured | Run once when the container is made, and again on each Rebuild, as the container's user in the work folder, before the workspace is ready. A string runs in a shell, an array as a program, and the entries of an object one after another. A command that takes too long is stopped. |
| `updateContentCommand` | honoured | Run once when the container is made, and again on each Rebuild, as the container's user in the work folder, before the workspace is ready. A string runs in a shell, an array as a program, and the entries of an object one after another. A command that takes too long is stopped. |
| `postCreateCommand` | honoured | Run once when the container is made, and again on each Rebuild, as the container's user in the work folder, before the workspace is ready. A string runs in a shell, an array as a program, and the entries of an object one after another. A command that takes too long is stopped. |
| `postStartCommand` | honoured | Run as the create commands are, and again each time the workspace's container is started, before its Claude server is. |
| `userEnvProbe` | honoured | Accepted but never run: commands get the environment of the file and the image, not one probed from a shell. |
| `build` | honoured | rcman builds the image in a sandbox of its own, from a copy of the repository without its git folder, as the workspace is made. |
| `build.dockerfile` | honoured | The Dockerfile to build, a path in the repository relative to the folder of devcontainer.json. It may not leave the repository. |
| `build.context` | honoured | The folder the build is run in, relative to the folder of devcontainer.json and inside the repository; the Dockerfile's folder when not set. |
| `build.args` | honoured | Build arguments, an object of names and string values. |
| `build.target` | honoured | The stage of the Dockerfile to build. |
| `dockerFile` | honoured | The older form of build.dockerfile. |
| `context` | honoured | The older form of build.context, used with dockerFile. |
| `features` | honoured | Installed on the image by the builder: a feature of the repository ('./name' in the folder of devcontainer.json), of a registry (a reference, such as ghcr.io/owner/features/name:tag) or at an https address. What a feature asks of its container is held to this policy, naming the feature. |
| `overrideFeatureInstallOrder` | honoured | The order features are installed in. |

### Ignored

| Key | What rcman does | Why |
|---|---|---|
| `forwardPorts` | ignored | rcman publishes no port of a workspace. |
| `portsAttributes` | ignored | rcman publishes no port of a workspace. |
| `otherPortsAttributes` | ignored | rcman publishes no port of a workspace. |
| `appPort` | ignored | rcman publishes no port of a workspace. |
| `postAttachCommand` | ignored | Nothing attaches an editor to a workspace, so nothing runs on attach. |
| `customizations` | ignored | Settings of editors and tools, none of which rcman runs. |
| `shutdownAction` | ignored | A workspace is stopped and removed only by rcman and its owner. |
| `secrets` | ignored | rcman gives a workspace no secret a repository asks for. |
| `hostRequirements` | ignored | Requests for the size of the machine, which rcman does not check. A request for a GPU is refused. |
| `name` | ignored | A label for editors. |
| `$schema` | ignored | Only describes the file. |
| `extensions` | ignored | Settings of editors, from an older form of the file. |
| `settings` | ignored | Settings of editors, from an older form of the file. |
| `id` | ignored | Names a feature in the metadata of an image. |

### Refused

| Key | What rcman does | Why |
|---|---|---|
| `build.options` | refused | Options for the build command could undo the limits on the builder, so rcman gives it none. |
| `build.cacheFrom` | refused | Layers taken from other images would be pulled from registries of the repository's choosing, and rcman cannot sign in to a registry. |
| `mounts` | refused | rcman mounts nothing a repository asks for: a workspace's storage is its volume and its /tmp. |
| `workspaceMount` | refused | rcman decides where a workspace's files are. |
| `workspaceFolder` | refused | rcman decides where a workspace's files are. |
| `runArgs` | refused | Arguments for the container engine could undo the limits rcman puts on a workspace. |
| `initializeCommand` | refused | It would run on the machine rcman runs on, outside any workspace. |
| `dockerComposeFile` | refused | Workspaces of several containers are not supported. |
| `service` | refused | Workspaces of several containers are not supported. |
| `runServices` | refused | Workspaces of several containers are not supported. |
| `privileged` | refused | A privileged container would not be kept apart from the machine. |
| `capAdd` | refused | Extra capabilities would weaken the limits on a workspace. |
| `securityOpt` | refused | Security options of a repository's choosing would weaken the limits on a workspace. |
| `hostRequirements.gpu` | refused | A workspace has no GPU. |
| `overrideCommand=false` | refused | rcman runs its own process in the container, so the image's own command is not kept. |
| `entrypoint` | refused | An entrypoint of an image or a feature is not run: rcman starts its own process in the container. |

A file must name an image with `image` or a Dockerfile with `build`; one that names neither
is refused.

## Variables

The variables of the specification are filled in where rcman honours a key:
`${containerWorkspaceFolder}` (the workspace's work folder),
`${containerWorkspaceFolderBasename}`, `${devcontainerId}` (stable for the workspace, and
different for every workspace) and `${containerEnv:NAME}`, with an optional default after a
second colon. `${localEnv:NAME}` and `${env:NAME}` stand for their default, or nothing:
rcman never offers its own environment to a file. `${localWorkspaceFolder}` and its basename
name a folder of the machine rcman runs on, which a workspace has none of, so a file that
uses them is refused. No variable is filled in `image`: the name is what is pulled.

## Environment

`containerEnv` is set for the container and for every command rcman runs in the workspace,
Claude included; `remoteEnv` is set for those commands as well, over `containerEnv`, and a
null value removes a variable. The image's own variables are the base of both; for an image that was built, they include
what its features set in `containerEnv`, which the build bakes into the image. They are
stored with the workspace's plan and apply to git, tmux, Claude and the lifecycle commands.

A file may not set the names rcman sets itself in every workspace: the home folder, the user
and shell names, the terminal, Claude's configuration folder and settings, gh's
configuration folder (`GH_CONFIG_DIR`), and git's prompting and lock settings. Such a file is refused, naming the variable and the entry that
sets it. Nor may it set a variable that would move git's configuration from where rcman writes
it for the person: `XDG_CONFIG_HOME`, and the `GIT_CONFIG` ones (`GIT_CONFIG_GLOBAL`,
`GIT_CONFIG_SYSTEM`, `GIT_CONFIG_NOSYSTEM`, `GIT_CONFIG_COUNT`, `GIT_CONFIG_PARAMETERS` and the
numbered `GIT_CONFIG_KEY_n` and `GIT_CONFIG_VALUE_n`) or make git print what it sends
(`GIT_TRACE` and its kin, `GIT_CURL_VERBOSE`); an image's own are not taken. rcman's own git
runs with none of the `GIT_` variables a file sets. A file may
set `LANG`, `LC_ALL` and `BROWSER`, which rcman sets itself in
only some of its programs; there its value wins over the file's, and everywhere else,
lifecycle commands included, the file's is used. `PATH` may be set; rcman's own folders of
programs, Claude's and the `podman` and `docker` that run containers in a workspace (see
[Containers](workspaces.md#containers)), stay first in it. `GH_TOKEN` and `GITHUB_TOKEN` may be set too, and gh then uses
that token in place of the person's, which **Remove token** does not remove (see
[Git](git.md#gh)).

The deviations from the specification:

- `HOME` is the fixed home folder of the workspace, whatever the image's user has in its
  passwd entry, because Claude's and git's files live there on the workspace's volume.
- `userEnvProbe` is accepted but never run: commands get the environment of the file and
  the image, not one probed from a shell.

## The user and the shared sign-in

`remoteUser` names the user that rcman's commands and Claude run as; without it
`containerUser` does, else the image's own user. It must be in the image's passwd file,
which rcman reads without running the image; a name the image does not have is refused.

Every workspace of a person shares one Claude sign-in and one git configuration, files
that belong to the person's range of ids on this machine and are owned there by the user
that root is in the workspaces made from the default image. So that a user other than root
in a devcontainer image can use them, and the workspace's volume, rcman maps the image's
user onto that same owner and root onto the image user's: the user of a devcontainer
workspace is the same host identity as root in the person's other workspaces. No file is
given a new owner, and a sign-in refreshed in one workspace is the one every other uses.
`updateRemoteUserUID` is accepted and changes nothing, since no file needs a new owner.

A person's range gets its own copy of an image, made the first time a workspace of theirs
uses it. The first workspace from an image takes longer, and its copy takes space in rcman's
storage for every person who has used it. A person's copy is removed with the image, not
with their workspaces. An image that was built is removed with the workspace it was built
for (see [Disk](#disk)).

## The image

The image is named by `image`, as a plain registry reference (`host/path:tag`, optionally
`@digest`), as written. A name Podman would read as a transport or a file (`docker-archive:`,
`oci:`, `dir:` and the like) is refused, and so is a name in a registry on the machine rcman
runs on or on its network: `localhost` with a port, a host that is an address which is not a
public one (loopback, private and link-local ones among them, also written the short ways a
resolver reads, such as `127.1` or a single number), a name of one label with a port, or a
name that ends in one of `LOCAL_SUFFIXES` in `backend/rcman/devcontainer.py`. A pull runs in
the network of that machine, which a workspace's own network is kept from. A name that
resolves to such an address, and a registry that redirects to one, are not caught: that would
need the name resolved and the redirect followed. A name that begins `localhost/` with no
port names an image in rcman's storage and is never pulled.

- rcman pulls it itself, as its own account, with the pull's lines in the steps. An image
  that needs a sign-in to its registry cannot be pulled.
- An image already in rcman's storage is used when it cannot be pulled, so an image an
  administrator loaded there needs no registry.
- An image that carries a label beginning `rcman.` is refused: a container takes its image's
  labels, and those are the ones rcman finds its own containers by.
- rcman puts its own instructions for Claude at `/etc/claude-code/CLAUDE.md` in every
  workspace, read-only, in place of any file the image has there; the image's other files in
  `/etc/claude-code`, such as `managed-settings.json`, stay as they are. What they say depends
  on the image (see
  [What Claude is told in a workspace](workspaces.md#what-claude-is-told-in-a-workspace)): an
  image that carries the default image's label, `org.claude-rc-man.tools`, is told what the
  default image's workspaces are. Such an image is the default image itself, named in `image`
  by the name `RCMAN_WORKSPACE_IMAGE` holds (`localhost/rcman-workspace:<tag>`, in rcman's
  storage), or an image built from it elsewhere and pushed to a registry, which keeps the
  label. An image whose `/etc`
  or `/etc/claude-code` is a file, or whose `/etc/claude-code/CLAUDE.md` is anything but an
  ordinary file, is refused, read from the image before any container is made from it. A
  link there is judged by what it leads to.
- The image needs a **git** new enough for rcman (see [Git](git.md#images)), checked once the
  container runs, and **glibc**: an image built on musl, as Alpine ones are, is refused
  because Claude Code needs glibc. An image whose `/tmp` is a link or not a folder is refused,
  checked once the container runs: every workspace has a `/tmp` of its own (see
  [Containers](workspaces.md#containers)). Anything else rcman needs comes from rcman, not the image:
  tmux, busybox, Claude Code and rcman's other tools are mounted into every workspace.
- rcman starts its own process in the container, so the image's entrypoint and command are
  not run (`entrypoint` and `overrideCommand=false` are refused).
- A name that could stand for an image rcman built for a workspace (its repository name is
  reserved for that), or for an image by its id (a bare hexadecimal name), is refused in
  `image`: either would name an image that is not a registry's, and may be another
  person's.

## Building an image

A file with `build`, or with `dockerFile` (its older form), describes an image to build; one
with `features` has them installed on the image it names with `image`, or on the one it
builds. rcman builds with the Dev Containers CLI, the one the specification's reference
implementation is, in a **builder**: a container made for one build and removed after it,
which runs a Podman of its own, so that a Dockerfile's steps and a feature's scripts run
with no more than a workspace has. Which CLI and Node the builder image holds is pinned in
`installer/builder`, and the installer builds it (see
[Installation](installation.md)).

- **The sandbox.** The builder runs in a range of user ids of its own, which no person's
  workspace uses and which rcman's own identity never maps; the builds of different people
  may run at the same time in that one range, so it does not keep them apart from each
  other. It is not privileged: it has the one capability a Podman inside it needs to
  mount its own storage, and no device (`run_argv` in `backend/rcman/builder.py`), and a volume of its own for what it
  builds. Its network reaches everything outbound, as a workspace's does, the local network
  and the addresses cloud machines serve their metadata on included; only the machine's own
  loopback is blocked. It counts against the limits on memory, CPU and processes of the person who asked for the
  build, and has none of its own (see [Limits](users.md#limits)); disk cannot be limited. A
  hostile build that got out of its nested container would still be in the builder's own
  range, and the image it made is checked again before it is kept (see Copying out).
- **What it sees.** Only a copy of the workspace's files, streamed to it as a tar archive
  through rcman; nothing of the machine's is mounted. The workspace's own git folder, the
  one in the top folder, is not copied; a git folder deeper in the repository is, as any
  folder is. Symbolic links stay links, so a link that points outside the repository
  points into the builder, which holds nothing of the machine's. A repository bigger than
  `MAX_CONTEXT_BYTES` in `backend/rcman/builder.py` is not copied and fails the build.
- **What the CLI is given.** A configuration rcman writes beside the file's, with only the
  keys that describe the build: `build`, `image`, `features` and
  `overrideFeatureInstallOrder`. The CLI drops, without saying so, the keys it does not
  know, and does not refuse what rcman does (`runArgs`, `mounts`, `privileged`), so rcman
  holds the whole file to the policy itself before anything is built. The file's own
  lifecycle commands, user and environment are not given to the CLI: rcman applies them
  from the plan alone, so no command runs twice when a container is made. The CLI is run with `--no-lockfile`, so the
  copy of the repository is not changed.
- **Features.** A feature is one of the repository (`./name` in the folder of
  devcontainer.json), one in a registry that names its host, or one at an https address. What
  a feature asks of its container is read from the image's `devcontainer.metadata` label
  once the build is done, and held to the policy above before the image is copied out: a
  feature that asks for `privileged`, `capAdd`, `securityOpt`, `mounts` or an `entrypoint` is
  refused, naming the feature. The feature's `containerEnv` is not in the label; the build
  bakes it into the image's environment, from which the plan takes it.
- **The network and registries.** A build reaches the network the way a workspace does: the
  base image, a Dockerfile's `RUN` steps and features are fetched from registries and
  addresses on the internet, so the machine needs outbound access to them while it builds.
  rcman cannot sign in to a registry: an image, a base or a feature that needs a sign-in
  cannot be built on, and `build.cacheFrom` is refused for the same reason. A step that
  fetches packages from a mirror that is down may succeed without them; the build log is
  the place to see it.
- **Copying out.** The built image is saved by its id in the builder as an archive, which
  rcman stages in a private folder of its scratch area (the folder of the database, by
  default). rcman checks the archive before anything loads it (`verify_archive` in
  `backend/rcman/builder.py`): it holds exactly one image, under no name, and its
  configuration hashes to the id; nothing in it is extracted. Only then is it loaded into
  rcman's storage, where rcman names the image after the workspace and keeps one image for
  each. The label of the loaded image is held to the policy again when the workspace's plan
  is made from it. An image that was loaded and not kept is removed by its id. An image
  bigger than `MAX_IMAGE_BYTES` in the same file is not copied.
- **Progress.** The steps the build takes, and the copy, are lines of the workspace's
  creation log, or of its rebuild's, as an image pull's are, with what the CLI said redacted of the person's
  GitHub token.
- **Waiting.** The number of builds at once is bounded (`MAX_BUILDS` in
  `backend/rcman/workspaces.py`); a build beyond it waits for a place, with a line in its
  log saying so, and starts when one is free.
- **Time.** A build is stopped after `BUILD_SECONDS`, and the copy after `COPY_SECONDS`.
- **Deleting.** Deleting a workspace that is being built stops the build: rcman removes the
  builder, which ends everything running in it, and its volume.

### Disk

While a build runs, the builder's volume holds the base image, every layer of the build and
the cache of its steps, and the image is then saved, staged as an archive in the scratch area
and loaded: the space a build needs is a multiple of the size of the image, in the builder's
volume, in the scratch area for a while, and in rcman's storage. When it is done the builder,
its volume and the archive are removed; a staging folder an earlier rcman left is removed
when rcman starts.
What stays is one image in rcman's storage for each workspace that was built, and a copy of
it for each person's range that has run a workspace from it (see
[The user and the shared sign-in](#the-user-and-the-shared-sign-in)). While a workspace is
rebuilt its new image is built beside the old one, under a name of its own (`NEXT_TAG` in
`backend/rcman/runtime.py`), so for a while the storage holds both; the new one takes the
old one's name when the rebuild is done, and the old one is removed once nothing is made from
it. One that a rebuild cut short by rcman's restart left is removed when rcman starts. rcman
does not check for free space first: a build that runs out of it fails as below.

### When a build fails

A build that fails fails the workspace in plain words, with the end of what the builder said,
redacted of the person's GitHub token, in the reason and the log. The causes rcman tells
apart, from the words the builder used (`CAUSES` in `backend/rcman/builder.py`):

- a registry or an address that did not answer;
- an image or a feature that is not there (a name or tag that does not exist);
- a registry's refusal, which for rcman means an image or a feature that needs a sign-in
  or is not there;
- a Dockerfile step that failed;
- a feature that failed to install, naming it;
- the disk of the machine full;
- an image the build made that rcman does not accept, with the reason;
- the repository, or the image, bigger than the limits above;
- the build taking too long.

Any other failure says that the build could not be done, with the same end of the log. The
messages are `problems.DEVCONTAINER_MESSAGES`. The container, the builder, its volume and
the built image are all removed, leaving the failed entry only, as for a refused
configuration. A build that fails during a [Rebuild](#rebuilding-a-workspace) removes only the
builder, its volume and the new image: the workspace stays as it was.

## Lifecycle commands

`onCreateCommand`, `updateContentCommand` and `postCreateCommand` are the create commands.
They run when the workspace's container is made, in that order, as the specification runs
them, and the workspace is not ready until they are done: once when the workspace is made, and
again on each [Rebuild](#rebuilding-a-workspace), on the work folder they ran on before. They
should therefore be safe to run again (idempotent): a command that clones a second repository
or appends to a file should check first. `postStartCommand` runs after them, and again every
time the workspace's container is started: when a person's machine restarts and rcman brings
the workspace back, and when it is made again after a loss. A container that kept running
while rcman restarted is not started, so `postStartCommand` does not run for it.

- A command is run as the container's user in the work folder, with the environment above.
  A string is run by a shell, an array as a program, and the entries of an object one after
  another. A shell is the image's, else the one rcman mounts.
- Each command has a time limit (`HOOK_TIMEOUT_SECONDS` in `backend/rcman/workspaces.py`); a
  command that runs longer is stopped.
- What a command printed last is put in the workspace's steps, with the person's GitHub
  token removed from it if it printed one; only the end of each command's output is kept
  (`HOOK_KEEP_BYTES`).
- A create command that fails or times out fails the workspace, with its exit code and the
  end of its output. The container and the volume are **kept**; the person can rebuild the
  workspace, which runs its commands again on the same volume, or delete it, which removes
  them. During a Rebuild of a ready workspace a failing command puts the workspace back as it
  was instead (see below).
- When the container is made, at the workspace's making or at a Rebuild, `postStartCommand`
  is run as the create commands are, and fails the workspace, or the Rebuild, as they do. When
  the container is started again, a failing `postStartCommand` is logged and does not stop the
  workspace or its server.

**A security note.** The commands are the repository's, and the person chose them by choosing
the repository, or, at a Rebuild, by asking for it with the file that is in their workspace
then. They run inside the workspace with everything the workspace has, which
includes the person's GitHub token: git and gh read it from files that all of the
person's workspaces share, and any command in a workspace can read those files. Treat a
repository's lifecycle commands as the repository's code run with the person's token and
their Claude sign-in. rcman keeps them from other people's workspaces and from rcman, not
from the person's own.

## When a configuration is refused

A refused or invalid configuration, which is a refused or unknown key, a file that cannot
be read or parsed, an image that cannot be pulled, built, read or run, a feature that asks
for what is refused, or an image without a usable git or glibc or with a `/tmp` that is a
link or not a folder, fails the workspace and
removes its container and its volume, and the builder and the image built for it. A base
image built on musl, such as Alpine, is built and then refused, since the check for glibc
is made on the result. Only the failed
entry stays, with the reason in plain words, which names the key to change. A person fixes
the file, pushes the change, and makes the workspace again.

A failing create command is different: it keeps the container and the volume, as above. During
a Rebuild nothing is removed for a refusal: the workspace stays as it was, and the reason says
to change the file and rebuild again.

## Rebuilding a workspace

A changed devcontainer.json, or a changed Dockerfile it builds from, does not change a
workspace by itself: the workspace keeps the plan it was made from. rcman records a digest of
the file and its Dockerfile when the workspace is made and when it is rebuilt, and tells the
person when the file in the workspace's work folder differs from it; the person then asks
for a **Rebuild** (see [Workspaces](workspaces.md)), which makes the container again from what
the file asks for now, on the same volume, keeping what is in the work folder. The file read is
the one in the workspace as it is then, changed there or pulled. A workspace with no file is
rebuilt from rcman's current default image, so a Rebuild is also how a workspace moves to a
newer default image; a file removed makes the workspace from the default image again, and a
file added makes it from the file's image.

The digest covers the place the file was found in, what it holds and the Dockerfile it builds
from, and nothing else: a change to another file of the build's context, to a feature of the
repository's (`./name`), or a tag that names another image by now is not seen. A Rebuild asked
for anyway applies them.

A Rebuild takes these steps:

1. **Preparing.** The file is read and held to the policy, and the image it names is pulled, or
   the one it describes is built, beside the running container, which the person goes on
   using. A refusal or a failure here changes nothing: the reason is shown with the workspace.
2. **Switching.** The workspace's server, if it runs, is stopped, its terminal is ended and its
   container is discarded; meanwhile the workspace's server, git and terminal are refused.
   The person confirms this when they ask. The new container is made on the same volume, its
   git and glibc are checked, and every create command runs, then `postStartCommand`.
3. **Done.** The new plan is stored, the old image is removed once nothing is made from it, and
   the server is started again in the new container if it was running. Claude Code's sessions
   are those of the server started again; what was running in the old container, and its
   `/tmp`, are gone.

If anything fails while the container is replaced - the image cannot be run, has no usable git
or glibc, or a create command fails - the workspace is **put back**: its container is made
again from the image it was made from, its old create commands run again, then its
`postStartCommand`, and its server is started again. What the new commands wrote to the work
folder stays. The same holds when Podman does not answer while the old container is discarded.
If even putting it back fails, or the rebuild fails in a way rcman did not foresee once the
old container was being replaced, the workspace stays in the switch: its server, git and
terminal stay refused, no server runs in it, and its steps say that it is put back as soon as
rcman can. rcman tries again every little while, and when it starts: its container is made
again from the image it was made from, its old create commands run again, and its server is
started if it was running; if the image it was made from cannot be had by then, the workspace
fails as in [Restoring](#restoring), and may be rebuilt. Such a workspace does not hold up an
upgrade of rcman, whose restart puts it back too. Either way the workspace says what happened.

A workspace that failed because a create command failed or took too long, or because the image
it was made from cannot be had now, keeps its volume, and may be rebuilt too: it is read
through its own container, or one made from rcman's default image where it has none, made as
above, and is ready again when it is done. If that fails it stays failed, with the new reason,
and may be rebuilt again; nothing is put back, and its volume is never removed. A workspace
whose clone failed, whose file was refused when it was made, or that rcman's restart cut short
while it was made, cannot be rebuilt: delete it and make it again.

- **Limits.** The build of a Rebuild counts against the person's limits as any build does, and
  runs while the workspace's server and Claude still run in the old container, so the two share
  the person's memory limit (see [Limits](users.md#limits)); a limit set close to what a
  server uses can make the build, or the server, fail.
- **Users.** rcman maps a workspace's user onto the same host user as root has in the person's
  other workspaces (see [The user and the shared sign-in](#the-user-and-the-shared-sign-in)),
  and leaves every other id as it is. A Rebuild that changes `remoteUser`, or moves between the
  default image and a file, changes which id inside the container is that host user: files the
  old user owned stay owned by the host user the new one is, but they show, inside the
  container, under the id that host user now has. From the default image (root) to a user with
  uid 1000, files root wrote show as the new user's and files of uid 1000 show as root's; the
  other way round, files of the image's user show as root's and files root wrote show as uid
  1000's. The work folder, the Claude sign-in and git's configuration stay the workspace user's
  in both directions; files of other ids in the volume, such as what a nested container stored,
  may need their owners changed. Every id stays within the person's range.
- **Upgrades.** A Rebuild is refused while rcman is upgraded, and an upgrade is refused while a
  workspace is rebuilt (see [Installation](installation.md)).
- **Restarts.** A Rebuild that rcman's restart cut short is settled when rcman starts: one that
  was preparing leaves the workspace as it was; one that was replacing the container makes it
  again from the image it was made from, with its create commands run again, whatever container
  was there, since one of the same image may already be the new one, and stays in the switch,
  tried again every little while, until that is done; one that had stored its new
  plan is finished. The Rebuild of a failed workspace that a restart cut short leaves it
  failed, and it may be rebuilt again. The workspace says so. A Rebuild that stored its new plan but could not
  finish renaming the image it built is finished by rcman shortly afterwards, without a
  restart.

## Restoring

The plan (the image, the user, the environment and the commands) is stored with the
workspace, with the id of the image it was made from, and the digest where the image came from a
registry. When the machine restarts and a workspace's container is gone, rcman makes it again
from the stored plan on the same volume, from the image of that id: a tag that names another
image by then changes nothing, and nothing is pulled while the image is in rcman's storage. A
workspace made from rcman's own image is made again from the image it was made from, not from a
newer one. If rcman's storage lacks the image, it is pulled by its digest where
there is one; if it cannot be had, the workspace fails and says so, and may be rebuilt or
deleted. An image that was built is never built again nor pulled: one that is missing from
rcman's storage fails the workspace, which says so. Restoring also removes what no workspace
needs: builders left by a build and their volumes, built images that no stored plan names, an
image a Rebuild left, and the other images in rcman's storage that nothing refers to, such as
images pulled for workspaces that are gone and older default images; it does so after a
workspace or a person is deleted, and after a Rebuild, too, and never removes an image a
container is made from. It keeps every image a stored plan or a workspace names, by name and by
id, the current default image and any default image built after it, which an upgrade builds
before rcman restarts, the builder images with every tag they have, and an image with no name for
a while after it was made. An older default image stays only while a workspace or a stored plan
made from it remains; where the current default image is missing from rcman's storage, every
default image stays. It removes nothing at all when the table of workspaces is empty
while rcman's Podman holds some, which it writes to the [journal](journal.md) as an error. The
create commands are not run again, except for a Rebuild cut short while the container was
replaced (see [Rebuilding a workspace](#rebuilding-a-workspace)). A workspace that rcman's restart interrupted while it was
being made fails and says so; it is not made again.

# devcontainer.json support

A repository can say, in a devcontainer.json, which image its workspace should run in and
what to run before the workspace is ready. When a workspace is made by cloning a repository
(see [Workspaces](workspaces.md)), rcman reads that file, holds it to the policy below,
pulls the image it names, and makes the workspace's container from that image instead of
from the default one ([`RCMAN_WORKSPACE_IMAGE`](configuration.md)). A repository without
the file gets the default image. A workspace made empty never has a file to read.

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
label: a refused or unknown key in it fails the workspace, naming the key and the entry.

### Honoured

| Key | What rcman does | Why |
|---|---|---|
| `image` | honoured | The image is pulled by rcman, and the workspace's container is made from it. |
| `remoteUser` | honoured | The user rcman's commands and Claude run as. It must be in the image, and it is mapped onto the same host user as root has in your other workspaces. |
| `containerUser` | honoured | Used as remoteUser when that is not set. |
| `updateRemoteUserUID` | honoured | Accepted. rcman always maps the user onto the same host user as root, so no file needs a new owner. |
| `containerEnv` | honoured | Set for the container and for every command rcman runs in the workspace, Claude included. |
| `remoteEnv` | honoured | Set for every command rcman runs in the workspace, Claude included; a null value removes a variable. |
| `init` | honoured | Accepted. Every workspace's container runs with an init process. |
| `waitFor` | honoured | Accepted. The workspace is ready only after all its create commands have run. |
| `overrideCommand` | honoured | Accepted when absent or true. rcman runs its own process in the container. |
| `onCreateCommand` | honoured | Run once, as the container's user in the work folder, before the workspace is ready. A string runs in a shell, an array as a program, and the entries of an object one after another. A command that takes too long is stopped. |
| `updateContentCommand` | honoured | Run once, as the container's user in the work folder, before the workspace is ready. A string runs in a shell, an array as a program, and the entries of an object one after another. A command that takes too long is stopped. |
| `postCreateCommand` | honoured | Run once, as the container's user in the work folder, before the workspace is ready. A string runs in a shell, an array as a program, and the entries of an object one after another. A command that takes too long is stopped. |
| `postStartCommand` | honoured | Run as the create commands are, and again each time the workspace's container is started, before its Claude server is. |
| `userEnvProbe` | honoured | Accepted but never run: commands get the environment of the file and the image, not one probed from a shell. |

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
| `build` | refused | rcman cannot build images yet; name a ready-made image with image. |
| `build.options` | refused | Options for building an image, which rcman does not do. |
| `dockerFile` | refused | rcman cannot build images yet; name a ready-made image with image. |
| `context` | refused | rcman cannot build images yet; name a ready-made image with image. |
| `features` | refused | Features need an image to be built, which rcman cannot do yet; name a ready-made image with image. |
| `overrideFeatureInstallOrder` | refused | It orders features, which rcman cannot install yet. |
| `mounts` | refused | A workspace's volume is its only storage; rcman mounts nothing else a repository asks for. |
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

`build`, `dockerFile`, `context` and `features` are refused until rcman can build images,
which a later step adds; until then a file must name a ready-made image with `image`, and a
file that names none is refused.

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
null value removes a variable. The image's own variables are the base of both. They are
stored with the workspace's plan and apply to git, tmux, Claude and the lifecycle commands.

A file may not set the names rcman sets itself in every workspace: the home folder, the user
and shell names, the terminal, Claude's configuration folder and settings, and git's
prompting and lock settings. Such a file is refused, naming the variable and the entry that
sets it. A file may set `LANG`, `LC_ALL` and `BROWSER`, which rcman sets itself in
only some of its programs; there its value wins over the file's, and everywhere else,
lifecycle commands included, the file's is used. `PATH` may be set; rcman's own folder of
programs stays first in it.

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
storage for every person who has used it. A person's image is removed with the image, not
with their workspaces.

## The image

The image is named by `image`, as a plain registry reference (`host/path:tag`, optionally
`@digest`), as written. A name Podman would read as a transport or a file (`docker-archive:`,
`oci:`, `dir:` and the like) is refused.

- rcman pulls it itself, as its own account, with the pull's lines in the steps. An image
  that needs a sign-in to its registry cannot be pulled.
- An image already in rcman's storage is used when it cannot be pulled, so an image an
  administrator loaded there needs no registry.
- The image needs a **git** new enough for rcman (see [Git](git.md#images)), checked once the
  container runs, and **glibc**: an image built on musl, as Alpine ones are, is refused
  because Claude Code needs glibc. Anything else rcman needs comes from rcman, not the image:
  tmux, busybox and Claude Code are mounted into every workspace.
- rcman starts its own process in the container, so the image's entrypoint and command are
  not run (`entrypoint` and `overrideCommand=false` are refused).

## Lifecycle commands

`onCreateCommand`, `updateContentCommand` and `postCreateCommand` are the create commands.
They run once, in that order, as the specification runs them, and the workspace is not ready
until they are done. `postStartCommand` runs after them, and again every time the
workspace's container is started: when a person's machine restarts and rcman brings the
workspace back, and when it is made again after a loss. A container that kept running while
rcman restarted is not started, so `postStartCommand` does not run for it.

- A command is run as the container's user in the work folder, with the environment above.
  A string is run by a shell, an array as a program, and the entries of an object one after
  another. A shell is the image's, else the one rcman mounts.
- Each command has a time limit (`HOOK_TIMEOUT_SECONDS` in `backend/rcman/workspaces.py`); a
  command that runs longer is stopped.
- What a command printed last is put in the workspace's steps, with the person's GitHub
  token removed from it if it printed one; only the end of each command's output is kept
  (`HOOK_KEEP_BYTES`).
- A create command that fails or times out fails the workspace, with its exit code and the
  end of its output. The container and the volume are **kept** so the person can see what
  happened; **Delete workspace** removes them.
- A failing `postStartCommand` is logged and does not stop the workspace or its server.

**A security note.** The commands are the repository's, and the person chose them by choosing
the repository. They run inside the workspace with everything the workspace has, which
includes the person's GitHub token: git reads it from the credential file that all of the
person's workspaces share, and any command in a workspace can read that file. Treat a
repository's lifecycle commands as the repository's code run with the person's token and
their Claude sign-in. rcman keeps them from other people's workspaces and from rcman, not
from the person's own.

## When a configuration is refused

A refused or invalid configuration, which is a refused or unknown key, a file that cannot
be read or parsed, an image that cannot be pulled, read or run, or an image without a usable
git or glibc, fails the workspace and removes its container and its volume. Only the failed
entry stays, with the reason in plain words, which names the key to change. A person fixes
the file, pushes the change, and makes the workspace again.

A failing create command is different: it keeps the container and the volume, as above.

## Changing the file, and restoring

A changed devcontainer.json applies only to workspaces made afterwards. A workspace made
earlier keeps the plan it was made from; a person who wants the change deletes the workspace
and makes it again, losing what is in it. A later step will apply a changed file to an
existing workspace.

The plan (the image, the user, the environment and the commands) is stored with the
workspace. When the machine restarts and a workspace's container is gone, rcman makes it
again from the stored plan on the same volume, pulling the image again when rcman's storage no
longer has it; if the image cannot be had, the workspace fails and says to delete it and make
it again. The create commands are not run again. A workspace that rcman's restart interrupted
while it was being made fails and says so; it is not made again.

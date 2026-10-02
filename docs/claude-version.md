# Claude Code versions

Everyone using rcman runs the same version of Claude Code: the **approved**
version. Claude Code's own self-update is turned off for them, so a new
version arrives only when an administrator rolls it out.

rcman also knows the **tested** version: the one its own recorded tests were
made with. Rolling out a different version is allowed, but the parts of Claude
Code that need a signed-in account — the folder-trust question and the server
screens — have then not been checked against it.

Administrators can do everything below on the **Claude Code** page in rcman,
or over SSH with `rcman-user`.

## Seeing what is installed

```
sudo rcman-user claude-status
```

shows the approved and tested versions and the version each person has,
marking anyone whose version differs.

In rcman, administrators are also warned at the top of every page while anyone's
version differs from the approved one, or while the approved version is not the
tested one, with a link to the **Claude Code** page — or, when rcman could not
check the versions, with the reason.

## Rolling out a version

```
sudo rcman-user claude-upgrade <version>
```

1. The version is installed in a scratch area that belongs to no person, and
   checked there without any Claude account: its version number, the account
   status it reports, how it refuses to start a server when nobody is signed
   in, its sign-in screen, the sign-in options rcman uses, and signing out.
2. If any check fails, nothing changes, and the failed checks are named.
3. If all pass, the version becomes the approved one and is installed for
   everyone whose version differs. Anyone whose installation fails is named;
   run `sudo rcman-user add` for them again to retry. People added later get
   the approved version.

Servers that are running keep the version they started with until they are
stopped and started again. An earlier version can be rolled out the same way.

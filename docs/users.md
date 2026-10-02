# Adding and removing people

Each person who uses rcman has their own Linux account on this machine and
connects their own Claude account. Administrators add and remove people with
`rcman-user`, run through sudo over SSH, or on the **People** page in rcman,
under **Admin** in the navigation, which asks the same question about the home folder. Both use `rc-admin`, which
never asks questions and answers in JSON.

`/etc/rcman/users.toml` is the one list of people: each entry maps a Google
email address to a Linux user and says whether they are an administrator. The
list of email addresses allowed to sign in is generated from it on every
change. Change the list with these commands rather than by editing the file.

## Adding someone

```
sudo rcman-user add <linux-user> <google-email> [--admin]
```

This creates the Linux account with a private home folder, adds it to the
`rcusers` group, installs the approved version of Claude Code for it, creates
`~/projects`, and adds the person to the list. Claude Code's self-update is
turned off for them; rcman installs new versions.

To give rcman an account that already exists — your own, for example — add
`--adopt`. System accounts are never accepted.

Running `add` again for someone already added repairs their account: it adds
whatever is missing and reinstalls Claude Code if their version is not the
approved one. Run it again with or without `--admin` to change whether they
are an administrator.

## Removing someone

```
sudo rcman-user del [-d | --delete | --keep-home] <linux-user>
```

This stops the person's servers, signs their Claude account out on this
machine, stops everything still running under their account, removes the
Linux account and takes them off the list.

- `-d` or `--delete` also deletes their home folder.
- `--keep-home` keeps it.
- With neither, `rcman-user` asks, and keeps the home folder unless you answer
  yes. Without a terminal to ask on, it refuses.

You cannot remove yourself, and the last administrator cannot be removed.
People whose Linux account is not managed by rcman are refused.

## Seeing who is there

```
sudo rcman-user list
```

lists everyone with their Linux user, email, role, and whether their account
exists.

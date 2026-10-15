# Adding and removing people

Each person who uses rcman signs in with their Google address, and works in
workspaces of their own (see [Workspaces](workspaces.md)). rcman keeps the list
of people itself; only the addresses on it may sign in, and the list of
addresses oauth2-proxy lets through is written from it on every change.

Administrators add and remove people on the **People** page in rcman, under
**Admin** in the navigation, or with `rcman-admin` as root over SSH. Both do the
same thing; `rcman-admin` runs as rcman's own account whoever starts it.

## Adding someone

On the **People** page, give their Google address, and tick **Administrator**
to make them one. Or:

```
sudo rcman-admin add <google-email> [--admin]
```

They can sign in at once. Each person gets a range of user ids of their own,
which every workspace of theirs runs in, so no two people's workspaces share
one. rcman has room for as many people as it has ranges; past that, adding
someone is refused.

## Removing someone

On the **People** page, **Remove** asks first, then removes them. Or:

```
sudo rcman-admin delete <google-email>
```

From the moment the removal starts, they cannot sign in. **Every
workspace of theirs is deleted**, with everything in it and its server, and
their settings, their GitHub token and their Claude sign-in with them. Anyone may be removed — yourself and the last
administrator too: root can always add an administrator again with
`rcman-admin`.

## Seeing who is there

The **People** page lists everyone, with their role and how many workspaces
they have. So does:

```
sudo rcman-admin list
```

`rcman-admin --help` describes every command.

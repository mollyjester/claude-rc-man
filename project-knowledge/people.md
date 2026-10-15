# People and rcman-admin

Who may use rcman, kept by rcman itself. How administrators add and remove
people: [the user guide](../docs/users.md).

| Module | What it is |
|---|---|
| `backend/rcman/people.py` | The `people` table and the allowed list |
| `backend/rcman/identity.py` | Who is asking, checked against the table on every request |
| `backend/rcman/routers/people.py` | The People page's routes, for administrators |
| `backend/rcman/admin.py` | `rcman-admin`, the same from a shell |

## The table

`people` (`backend/rcman/db.py`) holds each person's address, unique whatever
its case and kept in lowercase; whether they are an administrator; their slot;
their state, `active` or `deleting`; their Claude account as their last sign-in
named it (`claude_account`, the `oauthAccount` Claude wrote, or null); and
whether a sign-out of it waits for their running servers (`claude_signing_out`;
see [backend.md](backend.md#claude-accounts)). An address is checked by `EMAIL` in
`helpers/rules.py`, the rule the installer checks the first administrator's by
too.

**The slot** is the uid range every workspace of the person runs in: slot `k`
maps a workspace's ids from 0 onto rcman's subordinate ids from `k × RANGE`
(see [runtime.md](runtime.md#the-runtime)). A person gets the lowest free slot
from 1 to `MAX_PEOPLE`; slot 0 is rcman's own ids, and the table refuses it.
No two people share a slot, so no two people's workspaces share a range. The
installer gives rcman enough subordinate ids for every slot
(`SUBORDINATE_COUNT` in `installer/install.py`), which a test holds to
`MAX_PEOPLE`. A slot a deleted person had is given to the next person added.

## The allowed list

oauth2-proxy lets in only the addresses in the allowed list,
`RCMAN_ALLOWED_EMAILS` (see [the configuration guide](../docs/configuration.md)):
every `active` person's address, one a line. `write_allowed` writes it whole,
to a file beside it, and renames that over it, so oauth2-proxy, which watches
the file, never reads half of one; it is rcman's, readable by its group, which
oauth2-proxy's account belongs to. rcman writes it as it starts, and again on
every change. rcman's database is its own alone (`db.MODE`, and `UMask=0077` in
`rcman.service`), though oauth2-proxy shares its group. rcman itself checks every
request against the table, so a
removal takes effect at once in rcman whatever oauth2-proxy has read.

## Deleting a person

`Workspaces.delete_person` (`backend/rcman/workspaces.py`) marks the person
`deleting`, from when they are let in nowhere, off the allowed list, and no
workspace is made for them (`not_allowed`); deletes
every workspace of theirs, container and volume, waiting for one still being
made; then their Claude sign-in and git folder (see
[runtime.md](runtime.md#a-persons-claude-sign-in)); then removes their row, their
settings and, with the row (`on delete cascade`), their GitHub token, and
writes the allowed list again. The sign-in and git folder go before the row
because ids and slots are given again: a file left behind would sign the next
person in, or give them the last one's token. One that cannot be removed leaves the row `deleting`, for
rcman's next start. A person with workspaces cannot be removed from the table: the
workspaces' rows refer to theirs. A deletion that could not remove a workspace answers `workspace_failed`
and leaves the person `deleting`, let in nowhere; that, and a deletion that
rcman's restart cut short, is finished when rcman next starts (see [runtime.md](runtime.md#what-rcman-keeps-and-brings-back)). Anyone may be deleted — oneself, and the
last administrator too: there is no such check, and root can always add an
administrator with `rcman-admin`.

A deleted person's copy of an image (see [runtime.md](runtime.md#the-runtime))
stays in Podman's storage until the image is removed.

## rcman-admin

`rcman-admin` is the project's script of that name (`pyproject.toml`), in the
release's virtualenv; the installer links it into `/usr/local/sbin` (see
[installer.md](installer.md)). Its verbs are `add`, `list` and `delete`;
`rcman-admin --help` is the reference. It reads the settings the service runs
with from its environment file, `ENV_FILE`, or the one `--env-file` names, and
migrates the database as the service does. Run as root, it runs itself again as
rcman with `runuser`, with rcman's own runtime folder and home, so that it
reaches rcman's Podman socket whatever root's shell had; deleting a person reaches Podman as rcman does. It answers
in words, and refuses with the problem's own words on stderr and exit status
1. The installer adds the first administrator with it before the services
start.

## Tests

| Tests | What they prove |
|---|---|
| `tests/test_db.py` | The schema: one address and one slot per person, no slot 0, no person removed while workspaces are theirs |
| `tests/test_accounts.py` | A person's sign-in removed before their row, and a leftover one never reaching the next person given the same id and slot |
| `tests/test_people.py` | Slots, addresses, the bound on people, the allowed list written whole by rename, and the People page's routes, deleting a person's workspaces and settings among them |
| `tests/test_identity.py` | Who is let in: an active person, by address in any case, and no one else from the moment they are removed or being deleted |
| `tests/test_admin.py` | `rcman-admin`'s verbs, its words, running itself again as rcman, and the installed script |
| `tests/container/test_install_in_container.py` | The installed thing: the first administrator, `rcman-admin` by name as root, the allowed list's owner and mode, and deleting a person leaving nothing labelled theirs |

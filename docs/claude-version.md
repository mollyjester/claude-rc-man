# Claude Code versions

Everyone using rcman runs the same version of Claude Code: the **approved**
version. Claude Code's own self-update is turned off for them, so a new
version arrives only when an administrator rolls it out.

rcman also knows the **tested** version: the one its own recorded tests were
made with. Rolling out a different version is allowed, but the parts of Claude
Code that need a signed-in account — the folder-trust question and the server
screens — have then not been checked against it.

Administrators can do everything below on the **Claude Code** page in rcman,
under **Admin** in the navigation, or over SSH with `rcman-user`.

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
   checked there without any Claude account, by each check in the table
   below.
2. If installing it or any check fails, nothing changes, and what failed is
   named, as the table names it. Under each name is its report: what was run
   and what came back — for `install`, the last lines the installer printed.
   A report is shown to administrators only: on the **Claude Code** page,
   under the refusal, and by `rcman-user` after it. It is cut to a limited
   number of lines, saying how many more were not shown — the installer's
   keeps its last lines, and says where earlier ones were left out — and
   each line to a limited length, ending in `…` where it was cut. Before it
   is cut, control characters are removed, and these are blanked with a
   placeholder in braces:
   - any run of text up to a space, a quote or an angle bracket that is part
     of a URL carrying something private, whatever its scheme, host or path:
     one that mentions `oauth` or `authorize` along with a `/`, `?`, `=` or
     `&`, as sign-in URLs do; one with `environment=`, as environment URLs
     have; and one with a query or fragment parameter, a name and `=` after
     `?`, `&` or `#`;
   - Claude tokens (`sk-ant-…`), and `Bearer` credentials;
   - the value of any field whose name ends in `Token` or `token`, such as
     `accessToken` and `refreshToken`, and of any field whose name starts
     with `org`, `organisation` or `organization` in any case, such as
     `orgId` and `orgName`, where its value is in quotes and its name is in
     the same quotes or none, as JSON, a Python dictionary or Node.js writes
     a field;
   - the value of a variable set as `NAME=value`, as in an environment,
     whose name ends in `token` in any case, such as `ACCESS_TOKEN`;
   - every UUID;
   - environment ids (`env_…`) and session ids (`cse_…`);
   - email addresses.

   On the terminal, a line as wide as the terminal is read together with the
   next, where the program wrapped what it wrote, so a secret split across
   the edge is blanked whole. And since what is shown can be what was
   written with parts of it written over, each value shaped like a
   credential — a long run of the characters tokens are made of, with both
   letters and digits and nothing percent-encoded — that the program wrote as
   part of one of these is blanked wherever the screen still shows it whole. Where it wrote more such values than a report
   blanks, its screen is left out of the report, which says so.

   A refusal on the **Claude Code** page is recorded in the
   [audit trail](audit.md), and the system journal with it, by its code,
   `claude_checks_failed`, never with the report.
3. If all pass, the version becomes the approved one and is installed for
   everyone whose version differs. Anyone whose installation fails is named;
   run `sudo rcman-user add` for them again to retry. People added later get
   the approved version.

| Check | What it checks |
|---|---|
| `install` | The version installs in the scratch area |
| `version` | The version number it reports is the one being rolled out |
| `auth_status` | The account status it reports says nobody is signed in |
| `logged_out_refusal` | It refuses to start a server when nobody is signed in, as rcman recognises |
| `sign_in_screen` | Its sign-in screen is one rcman recognises |
| `login_options` | It offers the sign-in options rcman uses |
| `logout` | Signing out succeeds |

Servers that are running keep the version they started with until they are
stopped and started again. An earlier version can be rolled out the same way.

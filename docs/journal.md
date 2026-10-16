# The system journal

rcman runs as the systemd service `rcman`, and what it writes goes to the
system journal on the server. Besides the service's own messages — starting,
stopping, and anything that went wrong inside rcman, such as a workspace it
could not make or bring back when it started, that it is waiting for Podman
to answer, or that it removed nothing when it started because its table of
workspaces is empty while Podman holds some (see
[Podman's volumes](installation.md#podmans-volumes-are-rcmans)) — it writes three kinds of event there, each as one line of JSON:

- **Audit events**: each request that changed something, as the
  [audit trail](audit.md) records it.
- **Errors reported by the page**: failures a person's browser saw that rcman
  itself could not, described [below](#errors-reported-by-the-page).
- **Output rcman does not recognise**: a server on a screen rcman does not
  recognise, or Claude Code answering in a way it does not, described
  [below](#output-rcman-does-not-recognise).

Among the service's own messages, those about Claude Code versions come from
the logger `rcman.claude_versions`: a version adopted as the approved one, a
version removed from the store, one that could not be had, or a store that could
not be looked into (see [Claude Code versions](claude-version.md)).

Only the audit events are also in the audit trail. The journal keeps what it
is given only within its own size limits; see
[how long events are kept](audit.md#how-long-events-are-kept).

Each request that reaches Caddy, the polls included, is also in Caddy's access
log, described in [Installing rcman](installation.md#caddy). It shows the
requests rcman answered, and the status Caddy gave when it could not reach
rcman.

## Reading it

Reading the journal needs root, or membership of the group `adm` or
`systemd-journal`:

```
sudo journalctl -u rcman --grep '"event": "client_error"'
sudo journalctl -u rcman --grep '"event": "unrecognised"'
sudo journalctl -u rcman --grep '"action": '
```

The first shows the errors reported by the page, the second the output rcman
does not recognise, and the third the audit events. Add `-o cat` to have each
line as bare JSON, without the journal's time and process in front of it, for
a tool such as `jq`; `--since` and `--until` narrow it to a time, and `-f`
follows new lines as they are written. Every time in a line is in UTC.

## Errors reported by the page

A failure rcman sent itself — a refusal, with its reason — is shown to the
person, and recorded in the audit trail when the request was one that changes
something. Some failures never reach rcman, so the page reports them itself:

- **`unreachable`**: the page could not reach rcman. Either no answer came at
  all — the connection dropped, the browser was offline, or a phone paused the
  page while it was in the background — or Caddy answered, with no answer from
  rcman, that it could not reach it, as it does while rcman restarts or is
  stopped. The person is told the page could not reach rcman.
- **`signed_out`**: the person's sign-in has ended, and the request was sent
  to sign in again instead of reaching rcman. The person is told to reload the
  page to sign in again.
- **`unexpected_response`**: an answer came that the page could not read —
  one that is not rcman's own, such as an error page from a proxy between the
  browser and Caddy. The person is told rcman answered in a way the page does
  not understand.
- **`page_error`**: a fault in the page itself, not in rcman or the way to it.
  The person is told the same as for `unexpected_response`. These are worth
  reporting to whoever maintains rcman, with the line.

The page keeps what it saw in the browser's memory until a request to rcman
works, and then sends it, one report at a time; the person sees nothing of
it. Alike failures — the same kind, request and status, and the same error —
are one report, counted, with when the first and the last were seen. The page
keeps only a limited number of different reports, `MAX_QUEUED` in
[`frontend/src/clientErrors.ts`](../frontend/src/clientErrors.ts), and leaves
out any more until those are sent. What it keeps is lost when the page is
reloaded or closed, so a failure the person gave up on by reloading is never
reported. The browser's console shows each failure as it happened, for
whoever is at the browser.

rcman accepts only a limited number of reports from a person at once and each minute
(`REPORTS_BURST` and `REPORTS_PER_MINUTE` in
[`backend/rcman/routers/client_errors.py`](../backend/rcman/routers/client_errors.py)); it
refuses the rest with the status 429 and writes nothing for them, so that a page that fails
without end cannot fill the journal.

rcman cuts any text in a report that is longer than it takes, to the lengths
in [`backend/rcman/routers/client_errors.py`](../backend/rcman/routers/client_errors.py),
and adds who sent it. A line holds:

| Field | What it is |
|---|---|
| `event` | `client_error` |
| `at` | When rcman wrote the line |
| `email` | The address of the person whose page sent it |
| `kind` | What kind of failure it was, from the list above |
| `method` | The request's method, or `null` for a `page_error` |
| `path` | The request's path, without its query, or `null` for a `page_error` |
| `status` | The status of the answer, or `null` when none came |
| `name` | The name of the error the page saw, such as `TypeError` |
| `message` | The error's message, such as the browser's `Failed to fetch` |
| `content_type` | The answer's content type, when one came |
| `page` | The page's path in rcman, without its query |
| `visibility` | Whether the page was in view, as the browser said: `visible` or `hidden` |
| `online` | Whether the browser thought it was online |
| `elapsed_ms` | How long the request took before it failed, in milliseconds, or `null` for a `page_error` |
| `count` | How many times the failure was seen |
| `first_at` | When it was first seen, in milliseconds since 1970, by the browser's clock |
| `last_at` | When it was last seen, in the same way |

## Output rcman does not recognise

rcman writes a line when something it relies on enters a state it does not
recognise:

- a server is on a screen rcman does not recognise — in the person's own
  list of workspaces, in **Everyone** or the administrators' warning, or after
  an action on it (see [Workspaces](workspaces.md#when-something-looks-unfamiliar));
- Claude Code answers in a way rcman does not recognise, such as when rcman asks
  it whether it is signed in before starting a server.

It writes the line once, when the state begins, and nothing more while it
lasts. A server is written again only once it has been seen in another state,
or not at all, and then on an unrecognised screen again; a call is written
again only once the same call by the same person, in the same workspace, has
worked and then failed that way again. rcman remembers what it has written
only while it runs, so after it restarts a state that is still there is
written again; two answers arriving out of order can also repeat a line.

A line never holds the screen or what Claude Code said: they show a person's
own work, which may be anything, and administrators never see a person's screen.
The person sees it themselves, with **Show screen** or where the page shows
the failure, and can tell an administrator what it said. The line counts its
lines and holds a fingerprint of them instead, so that lines about the same
output can be told apart from lines about different output:

| Field | What it is |
|---|---|
| `event` | `unrecognised` |
| `at` | When rcman wrote the line |
| `email` | The person's address |
| `code` | `server_unrecognised` for a server on such a screen; `claude_unrecognised_output` for an action that failed with output from Claude Code |
| `verb` | What showed it: `list` for a list of workspaces, or the action, such as `start` or `output` |
| `workspace` | The name of the workspace it is about |
| `lines` | How many lines the screen or the output had |
| `fingerprint` | The start of a SHA-256 hash of those lines, each without its trailing spaces: two lines with the same fingerprint are very likely about the same output |

If rcman cannot look at an answer for output it does not recognise, it writes a
line saying so, with the error, which is not JSON; that is a fault in rcman,
worth reporting to whoever maintains it. The request itself is not affected.

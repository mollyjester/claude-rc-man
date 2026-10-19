import { type FormEvent, useCallback, useEffect, useId, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { type ApiError, api, problemFrom } from "./api";
import { BranchBar } from "./BranchBar";
import { Caret } from "./Caret";
import { RefreshIcon } from "./Icons";
import { MessageBar } from "./MessageBar";
import { m } from "./messages";
import { asApiError, ProblemView, useFieldErrors } from "./ProblemView";
import { usePopup } from "./usePopup";

// How often the status line's "Checked …" is worked out again.
export const CHECKED_TICK_MS = 1000;

type GitFile = {
  path: string;
  change: string;
  from?: string;
  submodule?: boolean;
  selectable?: boolean;
};
type FileList = { files: GitFile[]; more: number };
export type Branch = {
  name: string | null;
  commit: string | null;
  upstream: string | null;
  ahead: number | null;
  behind: number | null;
};
export type GitStatus = {
  branch: Branch;
  merging: boolean;
  staged: FileList;
  not_staged: FileList;
  author: { name: string; email: string } | null;
  push_has_remote: boolean;
  push_to_github: boolean;
  github_token: boolean | null;
  push_ahead: number | null;
};
type Pushed = { branch: string; upstream: string | null; upstream_set: boolean };
// What /api/git/sync answers, as m.synced reads it.
type Synced = Parameters<typeof m.synced>[0];
type CommitPushed = {
  commit: string;
  push: Pushed | null;
  push_problem: Record<string, unknown> | null;
};
export type Branches = {
  local: { branches: { name: string; current: boolean; upstream: string | null }[]; more: number };
  remote: {
    branches: { name: string; local: string | null; taken_by: string | null }[];
    more: number;
  };
};
type Action =
  | "status"
  | "branches"
  | "stage"
  | "unstage"
  | "commit"
  | "switch"
  | "branch"
  | "fetch"
  | "sync"
  | "push";

// A repository's status: fetched when the section opens, every pollMs while it
// is open (pollMs 0: never by itself), and whenever refresh is called. An
// answer that arrives after a newer request was made is dropped, as is any
// that arrives once the section has closed. checkedAt is when the status was
// last answered, which a failure leaves as it was.
function useGitStatus(workspace: number, pollMs: number) {
  const [status, setStatus] = useState<GitStatus | null>(null);
  const [checkedAt, setCheckedAt] = useState<number | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const latest = useRef(0);

  const refresh = useCallback(async () => {
    const asked = ++latest.current;
    try {
      const answer = await api.get<GitStatus>("/api/git/status", { workspace });
      if (asked === latest.current) {
        setStatus(answer);
        setCheckedAt(Date.now());
        setError(null);
      }
    } catch (e) {
      if (asked === latest.current) setError(asApiError(e));
    }
  }, [workspace]);

  useEffect(() => {
    refresh();
    const timer = pollMs > 0 ? setInterval(refresh, pollMs) : undefined;
    return () => {
      clearInterval(timer);
      latest.current++;
    };
  }, [refresh, pollMs]);

  return { status, checkedAt, error, refresh };
}

// What deleting the workspace would lose that is not safe in git, which the dialog asking first
// says (src/Dashboard.tsx): the files with changes that are not committed, and the commits that
// are not pushed. A file in both lists counts once; the files a list did not show are counted
// as the server counts them.
export type Unsaved = { files: number; commits: number };

export function unsavedIn(status: GitStatus): Unsaved {
  const shown = new Set([...status.staged.files, ...status.not_staged.files].map((f) => f.path));
  return {
    files: shown.size + status.staged.more + status.not_staged.more,
    commits: status.push_ahead ?? 0,
  };
}

const selectable = (file: GitFile) => file.selectable !== false;

// A file's change: its letter, coloured, with its word as what it says and its
// title; a change the page has no letter for, as the server names it.
function ChangeMark({ change }: { change: string }) {
  const word = m.changes[change];
  const letter = m.changeLetters[change];
  if (word === undefined || letter === undefined) return <span className="change">{change}</span>;
  return (
    <span className="change" title={word}>
      <span className={`letter change-${change}`} aria-hidden="true">
        {letter}
      </span>
      <span className="visually-hidden">{word}</span>
    </span>
  );
}

// One of the two lists of files, folding under its heading: a disclosure that
// says how many files it holds, with the action on what is selected beside it,
// shown while the list has files. The list is open at first while it has
// files, opens when files come to it, and otherwise stays as it was left.
// Inside, a checkbox for each file that can be selected, and Select all.
// Exported for its test only.
export function Files({
  title,
  list,
  selected,
  onSelect,
  action,
  busy,
  onAct,
}: {
  title: string;
  list: FileList;
  selected: ReadonlySet<string>;
  onSelect: (selected: ReadonlySet<string>) => void;
  action: string;
  busy: boolean;
  onAct: () => void;
}) {
  const hasFiles = list.files.length > 0;
  const [open, setOpen] = useState(hasFiles);
  // Files coming to an empty list open it in the same render, so it is never
  // drawn closed with them first.
  const [had, setHad] = useState(hasFiles);
  if (had !== hasFiles) {
    setHad(hasFiles);
    if (hasFiles) setOpen(true);
  }
  const id = useId();
  const count = list.files.length + list.more;
  const choosable = list.files.filter(selectable).map((f) => f.path);
  const all = choosable.length > 0 && choosable.every((p) => selected.has(p));
  function toggle(path: string) {
    const next = new Set(selected);
    if (!next.delete(path)) next.add(path);
    onSelect(next);
  }
  return (
    // biome-ignore lint/a11y/useSemanticElements: a fieldset would give the same group, but brings a border, padding and margin the panel does not want, and a minimum width of its widest unbreakable content, so a long file name would stretch the whole group past the panel where in a div only the name overflows; a div with the role has none of these to undo
    <div role="group" aria-label={title} className="files">
      <div className="files-head">
        <h4>
          <button
            type="button"
            className="disclosure"
            aria-expanded={open}
            aria-controls={id}
            aria-label={m.listHeading(title, count)}
            onClick={() => setOpen((now) => !now)}
          >
            <Caret />
            <span>{title}</span>
            <span className="count">{count}</span>
          </button>
        </h4>
        {hasFiles && (
          <button
            type="button"
            className="subtle"
            disabled={busy || selected.size === 0}
            onClick={onAct}
          >
            {action}
          </button>
        )}
      </div>
      <div id={id} className="files-body" hidden={!open}>
        {!hasFiles ? (
          <p>{m.noFiles}</p>
        ) : (
          <>
            <label className="select-all">
              <input
                type="checkbox"
                checked={all}
                disabled={choosable.length === 0}
                onChange={() => onSelect(new Set(all ? [] : choosable))}
              />
              {m.selectAll}
            </label>
            <ul>
              {list.files.map((file, i) => (
                // biome-ignore lint/suspicious/noArrayIndexKey: two names that are not UTF-8 can read the same once decoded, so the path alone is not unique; a row keeps no state of its own
                <li key={`${file.path}:${i}`}>
                  {selectable(file) ? (
                    <label className="file">
                      <input
                        type="checkbox"
                        checked={selected.has(file.path)}
                        onChange={() => toggle(file.path)}
                      />
                      <span className="file-name">{file.path}</span>
                    </label>
                  ) : (
                    <span className="file">
                      <span className="no-checkbox" aria-hidden="true" />
                      <span className="file-name">{file.path}</span>
                    </span>
                  )}
                  {file.from !== undefined && (
                    <span className="file-note">{m.renamedFrom(file.from)}</span>
                  )}
                  {file.submodule && <span className="file-note">({m.submodule})</span>}
                  {!selectable(file) && <span className="file-note">{m.notSelectable}</span>}
                  <ChangeMark change={file.change} />
                </li>
              ))}
            </ul>
            {list.more > 0 && <p>{m.andMore(list.more)}</p>}
          </>
        )}
      </div>
    </div>
  );
}

// The rest of Commit's split button: its menu, which holds Commit & Push.
function CommitMenu({ busy, onCommitPush }: { busy: boolean; onCommitPush: () => void }) {
  const menu = usePopup("CommitMenu");
  return (
    <div className="popup-anchor" ref={menu.rootRef}>
      <button
        type="button"
        className="split-toggle"
        aria-label={m.commitMenu}
        title={m.commitMenu}
        disabled={busy}
        {...menu.buttonProps}
      >
        <Caret />
      </button>
      {menu.open && (
        <div className="popover menu-popover" {...menu.panelProps}>
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              menu.close(true);
              onCommitPush();
            }}
          >
            {m.commitPush}
          </button>
        </div>
      )}
    </div>
  );
}

// The muted line at the foot of the section: when the status was last
// answered, worked out again every CHECKED_TICK_MS while the section is
// shown, and Refresh changes.
function StatusLine({ checkedAt, onRefresh }: { checkedAt: number | null; onRefresh: () => void }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), CHECKED_TICK_MS);
    return () => clearInterval(timer);
  }, []);
  return (
    <p className="status-line">
      {checkedAt !== null && (
        <span>{m.checkedAgo(Math.max(0, Math.floor((now - checkedAt) / 1000)))}</span>
      )}
      <button
        type="button"
        className="icon-button subtle"
        aria-label={m.refreshStatus}
        title={m.refreshStatus}
        onClick={onRefresh}
      >
        <RefreshIcon />
      </button>
    </p>
  );
}

// The selection of a list, kept to the files it still lists.
function kept(selected: ReadonlySet<string>, list: FileList): ReadonlySet<string> {
  const listed = new Set(list.files.filter(selectable).map((f) => f.path));
  const next = new Set([...selected].filter((p) => listed.has(p)));
  return next.size === selected.size ? selected : next;
}

// Why nothing can be pushed from here, or null where a push can be tried: on
// a branch, with a remote to push to, and to GitHub only with a token stored,
// as the status says.
function pushReason(status: GitStatus): "branch" | "remote" | "token" | null {
  if (status.branch.name === null) return "branch";
  if (status.push_has_remote !== true) return "remote";
  if (status.push_to_github && status.github_token !== true) return "token";
  return null;
}

// Whether Push itself is worth offering: not where the branch pushes has it all
// already. Before the first commit, nothing is; Commit & Push makes one.
const hasCommit = (status: GitStatus) => status.branch.commit !== null;
const somethingToPush = (status: GitStatus) => status.push_ahead !== 0;
// Whether the branch and its upstream each have commits the other lacks, which a
// sync, a fast-forward and a push, cannot bring together.
const diverged = (status: GitStatus) =>
  (status.branch.ahead ?? 0) > 0 && (status.branch.behind ?? 0) > 0;

// The Changes section of a repository, a card: its heading with the branch bar
// beside it, the staged and not staged files, and at its foot Commit and its
// menu's Commit & Push, and the line saying when the status was checked. What is offered follows
// rcman's own rules — Commit only with something staged and an author name
// set, Push and Commit & Push only as pushReason allows; git's own refusals
// are shown as it gave them, beside what was tried.
export function Changes({ workspace, pollMs }: { workspace: number; pollMs: number }) {
  const { status, checkedAt, error: statusError, refresh } = useGitStatus(workspace, pollMs);
  const [branches, setBranches] = useState<Branches | null>(null);
  const [staged, setStaged] = useState<ReadonlySet<string>>(new Set());
  const [notStaged, setNotStaged] = useState<ReadonlySet<string>>(new Set());
  const [message, setMessage] = useState("");
  const [running, setRunning] = useState<Action | null>(null);
  const [errors, setErrors] = useState<Partial<Record<Action, ApiError>>>({});
  const [pushed, setPushed] = useState<Pushed | null>(null);
  const [notPushed, setNotPushed] = useState<string | null>(null);
  const [synced, setSynced] = useState<Synced | null>(null);
  const [switching, setSwitching] = useState("");
  const busy = running !== null;
  const commitFields = useFieldErrors(errors.commit ?? null);
  const commitHelp = useId();

  const listBranches = useCallback(async () => {
    try {
      setBranches(await api.get<Branches>("/api/git/branches", { workspace }));
      setErrors((e) => ({ ...e, branches: undefined }));
    } catch (e) {
      setErrors((current) => ({ ...current, branches: asApiError(e) }));
    }
  }, [workspace]);

  // A workspace with no repository in it, as a new one has none, is said quietly.
  const empty = statusError?.code === "not_a_repository";

  // The branches when the section opens, and again once a repository has come to be.
  useEffect(() => {
    if (!empty) listBranches();
  }, [listBranches, empty]);

  // The branches again when the current branch changes, as when Claude switches.
  const branchName = status?.branch.name;
  const lastBranch = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    if (status === null) return;
    if (lastBranch.current !== undefined && lastBranch.current !== branchName) listBranches();
    lastBranch.current = branchName;
  }, [status, branchName, listBranches]);

  useEffect(() => {
    if (status === null) return;
    setStaged((s) => kept(s, status.staged));
    setNotStaged((s) => kept(s, status.not_staged));
  }, [status]);

  // Runs an action, shows its refusal beside it, then lists everything again.
  async function act(action: Action, run: () => Promise<unknown>): Promise<boolean> {
    setRunning(action);
    setErrors((e) => ({ ...e, [action]: undefined }));
    let done = false;
    try {
      await run();
      done = true;
      setErrors({});
    } catch (e) {
      setErrors((current) => ({ ...current, [action]: asApiError(e) }));
    } finally {
      setRunning(null);
    }
    await Promise.all([refresh(), listBranches()]);
    return done;
  }

  const inOrder = (list: FileList, selected: ReadonlySet<string>) =>
    list.files.filter((f) => selected.has(f.path)).map((f) => f.path);

  async function stage() {
    if (!status) return;
    const files = inOrder(status.not_staged, notStaged);
    if (await act("stage", () => api.post("/api/git/stage", { workspace, files })))
      setNotStaged(new Set());
  }

  async function unstage() {
    if (!status) return;
    const files = inOrder(status.staged, staged);
    if (await act("unstage", () => api.post("/api/git/unstage", { workspace, files })))
      setStaged(new Set());
  }

  async function commit(event: FormEvent) {
    event.preventDefault();
    if (await act("commit", () => api.post("/api/git/commit", { workspace, message })))
      setMessage("");
  }

  async function push() {
    setPushed(null);
    setNotPushed(null);
    setSynced(null);
    await act("push", async () =>
      setPushed(await api.post<Pushed>("/api/git/push", { workspace })),
    );
  }

  // Commit & Push: a commit made whose push failed is a commit made, so the box
  // is emptied and both are said.
  async function commitPush() {
    setPushed(null);
    setNotPushed(null);
    setSynced(null);
    setErrors((current) => ({ ...current, push: undefined }));
    let answer: CommitPushed | null = null;
    await act("commit", async () => {
      answer = await api.post<CommitPushed>("/api/git/commit-push", { workspace, message });
    });
    const done = answer as CommitPushed | null;
    if (done === null) return;
    setMessage("");
    if (done.push !== null) setPushed(done.push);
    if (done.push_problem !== null) {
      setNotPushed(done.commit);
      const problem = problemFrom(done.push_problem, Number(done.push_problem.status) || 0);
      setErrors((current) => ({ ...current, push: problem }));
    }
  }

  // Sync: pulled and not pushed is pulled, so both are said, the push's problem as
  // the sync's.
  async function sync() {
    setPushed(null);
    setNotPushed(null);
    setSynced(null);
    let answer: Synced | null = null;
    await act("sync", async () => {
      answer = await api.post<Synced>("/api/git/sync", { workspace });
    });
    const done = answer as Synced | null;
    if (done === null) return;
    setSynced(done);
    if (done.push_problem !== null) {
      const problem = problemFrom(done.push_problem, Number(done.push_problem.status) || 0);
      setErrors((current) => ({ ...current, sync: problem }));
    }
  }

  const createBranch = (name: string) =>
    act("branch", () => api.post("/api/git/branch", { workspace, name }));

  // New branch opened or closed, in whatever way: its refusal goes.
  const forgetCreateError = useCallback(
    () => setErrors((e) => (e.branch === undefined ? e : { ...e, branch: undefined })),
    [],
  );

  function switchTo(branch: string, remote: boolean) {
    setSwitching(branch);
    return act("switch", () => api.post("/api/git/switch", { workspace, branch, remote }));
  }

  const reason = status === null ? null : pushReason(status);
  // A sync is offered only where the server would run it: not where the upstream is gone
  // (named, with no ahead or behind), nor during a merge, which a fast-forward refuses.
  const syncable =
    status !== null &&
    reason === null &&
    hasCommit(status) &&
    !diverged(status) &&
    !status.merging &&
    (status.branch.upstream === null ||
      (status.branch.ahead !== null && status.branch.behind !== null));

  if (empty) {
    return (
      <section className="card panel-section changes" aria-label={m.changesTitle}>
        <div className="changes-head">
          <h3>{m.changesTitle}</h3>
        </div>
        <p className="muted">{m.notARepository}</p>
        <div className="changes-foot">
          <StatusLine checkedAt={checkedAt} onRefresh={() => refresh()} />
        </div>
      </section>
    );
  }

  return (
    <section className="card panel-section changes" aria-label={m.changesTitle}>
      <div className="changes-head">
        <h3>{m.changesTitle}</h3>
        {(status !== null || branches !== null) && (
          <BranchBar
            branch={status?.branch ?? null}
            branches={branches}
            branchesError={errors.branches ?? null}
            busy={busy}
            fetching={running === "fetch"}
            syncable={syncable}
            syncing={running === "sync"}
            pushOffered={
              status !== null && reason === null && hasCommit(status) && somethingToPush(status)
            }
            createError={errors.branch ?? null}
            onSwitch={switchTo}
            onCreate={createBranch}
            onCreateForget={forgetCreateError}
            onFetch={() => act("fetch", () => api.post("/api/git/fetch", { workspace }))}
            onSync={sync}
            onPush={push}
          />
        )}
      </div>
      <ProblemView error={statusError} />
      <ProblemView error={errors.switch ?? null} labels={{ branch: m.switchTo(switching) }} />
      <ProblemView error={errors.fetch ?? null} />
      {synced !== null && (
        <MessageBar kind="success" role="status">
          <p>{m.synced(synced)}</p>
        </MessageBar>
      )}
      <ProblemView error={errors.sync ?? null} />
      {status === null && !statusError && <p>{m.loading}</p>}
      {status !== null && (
        <>
          {status.merging && (
            <MessageBar kind="warning">
              <p>{m.mergeInProgress}</p>
            </MessageBar>
          )}
          {diverged(status) && (
            <MessageBar kind="warning">
              <p>{m.diverged}</p>
            </MessageBar>
          )}
          {reason === "branch" ? (
            <MessageBar kind="info">
              <p>{m.pushNeedsBranch}</p>
            </MessageBar>
          ) : reason === "remote" ? (
            <MessageBar kind="info">
              <p>{m.pushNeedsRemote}</p>
            </MessageBar>
          ) : reason === "token" ? (
            <MessageBar kind="warning">
              <p>
                {m.pushNeedsToken.before}
                <Link to="/settings">{m.pushNeedsToken.link}</Link>
                {m.pushNeedsToken.after}
              </p>
            </MessageBar>
          ) : (
            !hasCommit(status) && (
              <MessageBar kind="info">
                <p>{m.pushNeedsCommit}</p>
              </MessageBar>
            )
          )}
          {pushed !== null && pushed.upstream !== null && (
            <MessageBar kind="success" role="status">
              <p>{m.pushed(pushed.branch, pushed.upstream)}</p>
            </MessageBar>
          )}
          {notPushed !== null && (
            <MessageBar kind="warning">
              <p>{m.committedNotPushed(notPushed.slice(0, 7))}</p>
            </MessageBar>
          )}
          <ProblemView error={errors.push ?? null} />
          <Files
            title={m.stagedTitle}
            list={status.staged}
            selected={staged}
            onSelect={setStaged}
            action={m.unstageSelected}
            busy={busy}
            onAct={unstage}
          />
          <ProblemView error={errors.unstage ?? null} labels={{ files: m.stagedTitle }} />
          <Files
            title={m.notStagedTitle}
            list={status.not_staged}
            selected={notStaged}
            onSelect={setNotStaged}
            action={m.stageSelected}
            busy={busy}
            onAct={stage}
          />
          <ProblemView error={errors.stage ?? null} labels={{ files: m.notStagedTitle }} />
        </>
      )}
      <div className="changes-foot">
        {status !== null &&
          (status.author === null ? (
            <MessageBar kind="warning">
              <p>
                {m.commitNeedsAuthor.before}
                <Link to="/settings">{m.commitNeedsAuthor.link}</Link>
                {m.commitNeedsAuthor.after}
              </p>
            </MessageBar>
          ) : status.staged.files.length === 0 ? (
            <p className="muted">{m.commitNothingStaged}</p>
          ) : (
            <form noValidate onSubmit={commit} ref={commitFields.form}>
              <label>
                {m.commitMessage}
                <textarea
                  name="message"
                  value={message}
                  onChange={(e) => setMessage(e.target.value)}
                  rows={3}
                  {...commitFields.input("message", commitHelp)}
                />
              </label>
              {commitFields.message("message")}
              <p id={commitHelp} className="muted help">
                {m.commitMessageHelp}
              </p>
              <div className="actions">
                <div className="split">
                  <button type="submit" className="primary" disabled={busy}>
                    {m.commit}
                  </button>
                  {reason === null && <CommitMenu busy={busy} onCommitPush={commitPush} />}
                </div>
              </div>
            </form>
          ))}
        {status !== null && (
          <ProblemView error={errors.commit ?? null} labels={{ message: m.commitMessage }} inline />
        )}
        <StatusLine checkedAt={checkedAt} onRefresh={() => refresh()} />
      </div>
    </section>
  );
}

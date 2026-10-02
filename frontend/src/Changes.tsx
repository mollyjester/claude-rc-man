import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { ApiError, api, problemFrom } from "./api";
import { m } from "./messages";
import { ProblemView, asApiError } from "./ProblemView";

type GitFile = { path: string; change: string; from?: string; submodule?: boolean; selectable?: boolean };
type FileList = { files: GitFile[]; more: number };
type Branch = { name: string | null; commit: string | null; upstream: string | null; ahead: number | null; behind: number | null };
export type GitStatus = {
  path: string;
  branch: Branch;
  staged: FileList;
  not_staged: FileList;
  author: { name: string; email: string } | null;
  push_to_github: boolean;
};
type Pushed = { path: string; branch: string; upstream: string | null; upstream_set: boolean };
type CommitPushed = { path: string; commit: string; push: Pushed | null; push_problem: Record<string, unknown> | null };
type Branches = {
  local: { branches: { name: string; current: boolean; upstream: string | null }[]; more: number };
  remote: { branches: { name: string; local: string | null }[]; more: number };
};
type Action = "status" | "branches" | "stage" | "unstage" | "commit" | "switch" | "branch" | "fetch" | "push" | "github";

// A repository's status: fetched when the section opens, every pollMs while it
// is open (pollMs 0: never by itself), and whenever refresh is called. An
// answer that arrives after a newer request was made is dropped, as is any
// that arrives once the section has closed.
function useGitStatus(path: string, pollMs: number) {
  const [status, setStatus] = useState<GitStatus | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const latest = useRef(0);

  const refresh = useCallback(async () => {
    const asked = ++latest.current;
    try {
      const answer = await api.get<GitStatus>("/api/git/status", { path });
      if (asked === latest.current) {
        setStatus(answer);
        setError(null);
      }
    } catch (e) {
      if (asked === latest.current) setError(asApiError(e));
    }
  }, [path]);

  useEffect(() => {
    refresh();
    const timer = pollMs > 0 ? setInterval(refresh, pollMs) : undefined;
    return () => {
      clearInterval(timer);
      latest.current++;
    };
  }, [refresh, pollMs]);

  return { status, error, refresh };
}

const selectable = (file: GitFile) => file.selectable !== false;

// One of the two lists of files, with a checkbox for each file that can be
// selected, Select all, and the action on what is selected.
function Files({
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
  const choosable = list.files.filter(selectable).map((f) => f.path);
  const all = choosable.length > 0 && choosable.every((p) => selected.has(p));
  function toggle(path: string) {
    const next = new Set(selected);
    if (!next.delete(path)) next.add(path);
    onSelect(next);
  }
  return (
    <div role="group" aria-label={title} className="files">
      <h4>{title}</h4>
      {list.files.length === 0 ? (
        <p>{m.noFiles}</p>
      ) : (
        <>
          <label>
            <input type="checkbox" checked={all} disabled={choosable.length === 0} onChange={() => onSelect(new Set(all ? [] : choosable))} />
            {m.selectAll}
          </label>
          <ul>
            {list.files.map((file, i) => (
              <li key={`${file.path}:${i}`}>
                {selectable(file) ? (
                  <label>
                    <input type="checkbox" checked={selected.has(file.path)} onChange={() => toggle(file.path)} />
                    <span className="file-name">{file.path}</span>
                  </label>
                ) : (
                  <span className="file-name">{file.path}</span>
                )}{" "}
                <span className="change">{m.changes[file.change] ?? file.change}</span>
                {file.from !== undefined && <> {m.renamedFrom(file.from)}</>}
                {file.submodule && <> ({m.submodule})</>}
                {!selectable(file) && <> {m.notSelectable}</>}
              </li>
            ))}
          </ul>
          {list.more > 0 && <p>{m.andMore(list.more)}</p>}
        </>
      )}
      <button type="button" className="secondary" disabled={busy || selected.size === 0} onClick={onAct}>
        {action}
      </button>
    </div>
  );
}

function BranchLine({ branch }: { branch: Branch }) {
  const where =
    branch.name === null
      ? m.detachedAt((branch.commit ?? "").slice(0, 7))
      : branch.commit === null
        ? m.noCommitsYet(branch.name)
        : m.onBranch(branch.name);
  return (
    <>
      <p>{where}</p>
      {branch.upstream !== null && <p>{m.tracking(branch.upstream, branch.ahead, branch.behind)}</p>}
    </>
  );
}

// The selection of a list, kept to the files it still lists.
function kept(selected: ReadonlySet<string>, list: FileList): ReadonlySet<string> {
  const listed = new Set(list.files.filter(selectable).map((f) => f.path));
  const next = new Set([...selected].filter((p) => listed.has(p)));
  return next.size === selected.size ? selected : next;
}

// Why nothing can be pushed from here, or null where a push can be tried: on
// a branch, and to GitHub only with a token stored.
function pushReason(status: GitStatus, token: boolean | null): "branch" | "token" | null {
  if (status.branch.name === null) return "branch";
  if (status.push_to_github && token !== true) return "token";
  return null;
}

// The Changes section of a repository: its branch, the staged and not staged
// files, Commit, Push and Commit & Push, and its branches. What is offered
// follows rcman's own rules — Commit only with something staged and an author
// name set, Push only as pushReason allows; git's own refusals are shown as it
// gave them, beside what was tried.
export function Changes({ path, pollMs }: { path: string; pollMs: number }) {
  const { status, error: statusError, refresh } = useGitStatus(path, pollMs);
  const [branches, setBranches] = useState<Branches | null>(null);
  const [staged, setStaged] = useState<ReadonlySet<string>>(new Set());
  const [notStaged, setNotStaged] = useState<ReadonlySet<string>>(new Set());
  const [message, setMessage] = useState("");
  const [newBranch, setNewBranch] = useState("");
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState<Partial<Record<Action, ApiError>>>({});
  const [token, setToken] = useState<boolean | null>(null);
  const [pushed, setPushed] = useState<Pushed | null>(null);
  const [notPushed, setNotPushed] = useState<string | null>(null);

  useEffect(() => {
    api.get<{ token: boolean }>("/api/github").then(
      (answer) => setToken(answer.token === true),
      (e) => setErrors((current) => ({ ...current, github: asApiError(e) })),
    );
  }, []);

  const listBranches = useCallback(async () => {
    try {
      setBranches(await api.get<Branches>("/api/git/branches", { path }));
      setErrors((e) => ({ ...e, branches: undefined }));
    } catch (e) {
      setErrors((current) => ({ ...current, branches: asApiError(e) }));
    }
  }, [path]);

  useEffect(() => {
    listBranches();
  }, [listBranches]);

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
    setBusy(true);
    setErrors((e) => ({ ...e, [action]: undefined }));
    let done = false;
    try {
      await run();
      done = true;
    } catch (e) {
      setErrors((current) => ({ ...current, [action]: asApiError(e) }));
    } finally {
      setBusy(false);
    }
    await Promise.all([refresh(), listBranches()]);
    return done;
  }

  const inOrder = (list: FileList, selected: ReadonlySet<string>) =>
    list.files.filter((f) => selected.has(f.path)).map((f) => f.path);

  async function stage() {
    if (!status) return;
    const files = inOrder(status.not_staged, notStaged);
    if (await act("stage", () => api.post("/api/git/stage", { path, files }))) setNotStaged(new Set());
  }

  async function unstage() {
    if (!status) return;
    const files = inOrder(status.staged, staged);
    if (await act("unstage", () => api.post("/api/git/unstage", { path, files }))) setStaged(new Set());
  }

  async function commit(event: FormEvent) {
    event.preventDefault();
    if (await act("commit", () => api.post("/api/git/commit", { path, message }))) setMessage("");
  }

  async function push() {
    setPushed(null);
    setNotPushed(null);
    await act("push", async () => setPushed(await api.post<Pushed>("/api/git/push", { path })));
  }

  // Commit & Push: a commit made whose push failed is a commit made, so the box
  // is emptied and both are said.
  async function commitPush() {
    setPushed(null);
    setNotPushed(null);
    setErrors((current) => ({ ...current, push: undefined }));
    let answer: CommitPushed | null = null;
    await act("commit", async () => {
      answer = await api.post<CommitPushed>("/api/git/commit-push", { path, message });
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

  async function createBranch(event: FormEvent) {
    event.preventDefault();
    if (await act("branch", () => api.post("/api/git/branch", { path, name: newBranch }))) setNewBranch("");
  }

  const switchTo = (branch: string, remote: boolean) => act("switch", () => api.post("/api/git/switch", { path, branch, remote }));

  return (
    <>
      <div className="actions">
        <button type="button" className="secondary" onClick={() => refresh()}>
          {m.refreshStatus}
        </button>
      </div>
      <ProblemView error={statusError} />
      {status === null && !statusError && <p>{m.loading}</p>}
      {status !== null && (
        <>
          <BranchLine branch={status.branch} />
          {pushReason(status, token) === "branch" ? (
            <p>{m.pushNeedsBranch}</p>
          ) : pushReason(status, token) === "token" ? (
            <p>
              {m.pushNeedsToken} <Link to="/settings">{m.pushTokenLink}</Link>
            </p>
          ) : (
            <div className="actions">
              <button type="button" className="secondary" disabled={busy} onClick={push}>
                {m.push}
              </button>
            </div>
          )}
          {pushed !== null && pushed.upstream !== null && <p role="status">{m.pushed(pushed.branch, pushed.upstream)}</p>}
          {notPushed !== null && <p>{m.committedNotPushed(notPushed.slice(0, 7))}</p>}
          <ProblemView error={errors.push ?? errors.github ?? null} />
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
          <h4>{m.commitTitle}</h4>
          {status.author === null ? (
            <p>
              {m.commitNeedsAuthor} <Link to="/settings">{m.commitAuthorLink}</Link>
            </p>
          ) : status.staged.files.length === 0 ? (
            <p>{m.commitNothingStaged}</p>
          ) : (
            <form noValidate onSubmit={commit}>
              <label>
                {m.commitMessage}
                <textarea value={message} onChange={(e) => setMessage(e.target.value)} rows={4} />
              </label>
              <div className="actions">
                <button type="submit" className="primary" disabled={busy}>
                  {m.commit}
                </button>
                {pushReason(status, token) === null && (
                  <button type="button" className="secondary" disabled={busy} onClick={commitPush}>
                    {m.commitPush}
                  </button>
                )}
              </div>
            </form>
          )}
          <ProblemView error={errors.commit ?? null} labels={{ message: m.commitMessage }} />
        </>
      )}
      <h4>{m.branchesTitle}</h4>
      <ProblemView error={errors.branches ?? null} />
      {branches && (
        <>
          <ul aria-label={m.localBranches} className="branches">
            {branches.local.branches.map((b) => (
              <li key={b.name}>
                <span className="branch-name">{b.name}</span>
                {b.upstream && <> → {b.upstream}</>}
                {b.current ? (
                  <> ({m.currentBranch})</>
                ) : (
                  <>
                    {" "}
                    <button type="button" className="secondary" disabled={busy} aria-label={m.switchTo(b.name)} onClick={() => switchTo(b.name, false)}>
                      {m.switchTo(b.name)}
                    </button>
                  </>
                )}
              </li>
            ))}
          </ul>
          {branches.local.more > 0 && <p>{m.andMore(branches.local.more)}</p>}
          <ul aria-label={m.remoteBranches} className="branches">
            {branches.remote.branches.map((b) => (
              <li key={b.name}>
                <span className="branch-name">{b.name}</span>
                {b.local === null && (
                  <>
                    {" "}
                    <button type="button" className="secondary" disabled={busy} aria-label={m.switchTo(b.name)} onClick={() => switchTo(b.name, true)}>
                      {m.switchTo(b.name)}
                    </button>
                  </>
                )}
              </li>
            ))}
          </ul>
          {branches.remote.more > 0 && <p>{m.andMore(branches.remote.more)}</p>}
        </>
      )}
      <ProblemView error={errors.switch ?? null} labels={{ branch: m.branchesTitle }} />
      <form noValidate onSubmit={createBranch}>
        <label>
          {m.newBranchName}
          <input type="text" value={newBranch} onChange={(e) => setNewBranch(e.target.value)} />
        </label>
        <div className="actions">
          <button type="submit" className="primary" disabled={busy}>
            {m.createBranch}
          </button>
        </div>
      </form>
      <ProblemView error={errors.branch ?? null} labels={{ name: m.newBranchName }} />
      <div className="actions">
        <button type="button" className="secondary" disabled={busy} onClick={() => act("fetch", () => api.post("/api/git/fetch", { path }))}>
          {m.refreshBranches}
        </button>
      </div>
      <ProblemView error={errors.fetch ?? null} />
    </>
  );
}

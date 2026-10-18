import {
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { ApiError, api } from "./api";
import { Caret } from "./Caret";
import { Changes, type GitStatus, type Unsaved, unsavedIn } from "./Changes";
import { Dialog } from "./Dialog";
import { BackIcon, PlusIcon, TrashIcon } from "./Icons";
import { m } from "./messages";
import { asApiError, ProblemView, useFieldErrors } from "./ProblemView";
import { ACTIONS, dotFor, ServerCard, StatusDot, type Workspace } from "./Servers";
import { Terminal } from "./Terminal";
import { usePopup } from "./usePopup";
import { failureSentence } from "./workspaceError";

// Where a workspace's panel is: the Workspaces page, with the workspace in the address.
const workspaceLink = (id: number | null) => ({
  pathname: "/",
  search: id === null ? "" : `?workspace=${id}`,
});

// Whether a workspace can be deleted now, as the server allows it: one that is ready or failed,
// and one being made from a clone, whose delete cancels the clone; or else why not.
function whyNotDeletable(workspace: Workspace): string | null {
  const { state } = workspace;
  if (state === "ready" || state === "failed") return null;
  if (state === "creating") return workspace.source === null ? m.cannotDeleteCreating : null;
  if (state === "deleting") return m.cannotDeleteDeleting;
  return m.cannotDeleteState(m.workspaceStates[state] ?? state);
}

// The Workspaces page: under its heading, with New workspace's + beside it, the person's
// workspaces, each with its trash button, and the selected workspace's panel beside them. The
// selected workspace is in the address. On a phone the list and the panel take turns: with a
// workspace selected, the panel, with a way back to the list and a menu to switch to another.
export function Dashboard({ pollMs }: { pollMs: number }) {
  const [workspaces, setWorkspaces] = useState<Workspace[] | null>(null);
  // The Claude Code version a server started now would run, null where none is approved.
  const [approved, setApproved] = useState<string | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  // What was done, shown until another workspace is selected.
  const [notice, setNotice] = useState<string | null>(null);
  // The dialog open: New workspace's, or the one a row's trash button opened, for its workspace.
  const [making, setMaking] = useState(false);
  const [removing, setRemoving] = useState<Workspace | null>(null);
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const heading = useId();
  const headingRef = useRef<HTMLHeadingElement>(null);
  // Set by a delete, whose trash button goes with its row: focus goes to the list's heading
  // once the page is drawn without it, after the dialog has given focus back.
  const refocus = useRef(false);
  useEffect(() => {
    if (!refocus.current) return;
    refocus.current = false;
    headingRef.current?.focus();
  });
  const asked = params.get("workspace");
  const selectedId = asked && /^\d+$/.test(asked) ? Number(asked) : null;

  // A poll's failure is shown until a later poll works; an answer older than one
  // asked for since is dropped.
  const latest = useRef(0);
  const refresh = useCallback(async () => {
    const mine = ++latest.current;
    try {
      const answer = await api.get<{ workspaces: Workspace[]; claude_approved: string | null }>(
        "/api/workspaces",
      );
      if (mine === latest.current) {
        setWorkspaces(answer.workspaces);
        setApproved(answer.claude_approved);
        setError(null);
      }
    } catch (e) {
      const failed = asApiError(e);
      if (mine === latest.current) setError(failed);
    }
  }, []);

  useEffect(() => {
    refresh();
    if (pollMs > 0) {
      const timer = setInterval(refresh, pollMs);
      return () => clearInterval(timer);
    }
  }, [pollMs, refresh]);

  useEffect(() => {
    if (asked !== null) setNotice(null);
  }, [asked]);

  function select(id: number) {
    if (id !== selectedId) navigate(workspaceLink(id));
  }

  function made(workspace: Workspace) {
    setMaking(false);
    setWorkspaces((current) =>
      current && !current.some((w) => w.id === workspace.id) ? [...current, workspace] : current,
    );
    navigate(workspaceLink(workspace.id));
    refresh();
  }

  // A workspace that is not the selected one leaves the selection as it is.
  function deleted(workspace: Workspace) {
    setRemoving(null);
    setWorkspaces((current) => current?.filter((w) => w.id !== workspace.id) ?? null);
    if (workspace.id === selectedId) navigate(workspaceLink(null), { replace: true });
    setNotice(m.workspaceDeleted(workspace.name));
    refocus.current = true;
    refresh();
  }

  const selected = workspaces?.find((w) => w.id === selectedId);
  let shown: ReactNode;
  if (asked === null) shown = <p>{m.workspacePanelEmpty}</p>;
  else if (workspaces === null) shown = <p>{m.loading}</p>;
  else if (!selected) shown = <p>{m.workspaceUnknown}</p>;

  return (
    <>
      <div className="page-head">
        <h1 id={heading} tabIndex={-1} ref={headingRef}>
          {m.navDashboard}
        </h1>
        <button
          type="button"
          className="icon-button"
          aria-label={m.newWorkspace}
          title={m.newWorkspace}
          onClick={() => setMaking(true)}
        >
          <PlusIcon />
        </button>
      </div>
      <ProblemView error={error} />
      {notice && <p role="status">{notice}</p>}
      <div className={asked === null ? "dashboard" : "dashboard showing"}>
        <WorkspaceList
          labelledBy={heading}
          workspaces={workspaces}
          selected={selectedId}
          onSelect={select}
          onRemove={setRemoving}
        />
        <div className="panel-side">
          {asked !== null && (
            <PanelBar workspaces={workspaces ?? []} selected={selectedId} onSelect={select} />
          )}
          {selected ? (
            <WorkspacePanel
              key={selected.id}
              workspace={selected}
              approved={approved}
              pollMs={pollMs}
              onChange={refresh}
            />
          ) : (
            <section className="panel">{shown}</section>
          )}
        </div>
      </div>
      {making && <NewWorkspace onMade={made} onCancel={() => setMaking(false)} />}
      {removing &&
        (whyNotDeletable(removing) === null ? (
          <DeleteWorkspaceDialog
            key={removing.id}
            workspace={removing}
            onDeleted={() => deleted(removing)}
            onFailed={refresh}
            onCancel={() => setRemoving(null)}
          />
        ) : (
          <CannotDeleteDialog
            key={removing.id}
            workspace={removing}
            reason={whyNotDeletable(removing) ?? ""}
            onClose={() => setRemoving(null)}
          />
        ))}
    </>
  );
}

// Each workspace, selecting it, with its dot and, while it is not ready, its state, and its
// trash button, named after it; the list is named by the page's heading.
function WorkspaceList({
  labelledBy,
  workspaces,
  selected,
  onSelect,
  onRemove,
}: {
  labelledBy: string;
  workspaces: Workspace[] | null;
  selected: number | null;
  onSelect: (id: number) => void;
  onRemove: (workspace: Workspace) => void;
}) {
  return (
    <section aria-labelledby={labelledBy} className="list">
      {workspaces?.length === 0 && <p>{m.workspacesEmpty}</p>}
      {workspaces && workspaces.length > 0 && (
        <ul className="workspaces">
          {workspaces.map((workspace) => (
            <WorkspaceEntry
              key={workspace.id}
              workspace={workspace}
              current={workspace.id === selected}
              onSelect={onSelect}
              onRemove={onRemove}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

function WorkspaceEntry({
  workspace,
  current,
  onSelect,
  onRemove,
}: {
  workspace: Workspace;
  current: boolean;
  onSelect: (id: number) => void;
  onRemove: (workspace: Workspace) => void;
}) {
  const id = useId();
  const dot = dotFor(workspace);
  return (
    <li>
      <button
        type="button"
        className="name"
        aria-current={current ? "true" : undefined}
        aria-describedby={dot ? id : undefined}
        onClick={() => onSelect(workspace.id)}
      >
        {workspace.name}
      </button>
      {dot && <StatusDot kind={dot} id={id} />}
      {workspace.state !== "ready" && (
        <span className="muted"> {m.workspaceStates[workspace.state] ?? workspace.state}</span>
      )}
      <button
        type="button"
        className="icon-button secondary-danger trash"
        aria-label={m.deleteWorkspaceNamed(workspace.name)}
        title={m.deleteWorkspaceNamed(workspace.name)}
        onClick={() => onRemove(workspace)}
      >
        <TrashIcon />
      </button>
    </li>
  );
}

// Over the panel, where the list is not beside it, as on a phone: back to the list, and a menu
// of the workspaces to switch to another. The stylesheet shows it only there.
function PanelBar({
  workspaces,
  selected,
  onSelect,
}: {
  workspaces: Workspace[];
  selected: number | null;
  onSelect: (id: number) => void;
}) {
  const menu = usePopup("PanelBar");
  return (
    <div className="panel-bar">
      <Link to={workspaceLink(null)} className="button back">
        <BackIcon />
        {m.backToWorkspaces}
      </Link>
      <div className="menu switcher" ref={menu.rootRef}>
        <button type="button" {...menu.buttonProps}>
          {m.switchWorkspace}
          <Caret />
        </button>
        <ul {...menu.panelProps} hidden={!menu.open}>
          {workspaces.map((workspace) => (
            <li key={workspace.id}>
              <button
                type="button"
                aria-current={workspace.id === selected ? "true" : undefined}
                onClick={() => {
                  menu.close();
                  onSelect(workspace.id);
                }}
              >
                {workspace.name}
                {workspace.state !== "ready" && (
                  <span className="muted">
                    {" "}
                    {m.workspaceStates[workspace.state] ?? workspace.state}
                  </span>
                )}
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

// New workspace's dialog: makes a workspace with the name typed, empty or, with a GitHub
// address, cloned from it; the server judges both, and names a cloned workspace after its
// repository when none is typed. A refusal keeps the dialog open with what was typed.
function NewWorkspace({
  onMade,
  onCancel,
}: {
  onMade: (workspace: Workspace) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const nameHelp = useId();
  const urlHelp = useId();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const fields = useFieldErrors(error);

  async function create(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    // A space pasted around either is not part of it.
    const typedName = name.trim();
    const typedUrl = url.trim();
    try {
      const answer = typedUrl
        ? await api.post<{ workspace: Workspace }>("/api/git/clone", {
            url: typedUrl,
            ...(typedName ? { name: typedName } : {}),
          })
        : await api.post<{ workspace: Workspace }>("/api/workspaces", { name: typedName });
      onMade(answer.workspace);
    } catch (e) {
      setError(asApiError(e));
      setBusy(false);
    }
  }

  return (
    <Dialog owner="NewWorkspace" title={m.newWorkspace} onClose={onCancel} closable={!busy}>
      <form noValidate onSubmit={create} ref={fields.form}>
        <label>
          {m.newWorkspaceName}
          <input
            type="text"
            name="name"
            value={name}
            placeholder={m.examples.workspaceName}
            onChange={(e) => setName(e.target.value)}
            {...fields.input("name", nameHelp)}
          />
        </label>
        {fields.message("name")}
        <p id={nameHelp} className="muted">
          {m.newWorkspaceNameHelp}
        </p>
        <label>
          {m.newWorkspaceSource}
          <input
            type="text"
            name="url"
            value={url}
            placeholder={m.examples.address}
            onChange={(e) => setUrl(e.target.value)}
            {...fields.input("url", urlHelp)}
          />
        </label>
        {fields.message("url")}
        <p id={urlHelp} className="muted">
          {m.newWorkspaceSourceHelp}
        </p>
        <div className="actions">
          <button type="submit" className="primary" disabled={busy}>
            {busy ? m.creating : m.create}
          </button>
          <button type="button" disabled={busy} onClick={onCancel}>
            {m.cancel}
          </button>
        </div>
        <ProblemView
          error={error}
          labels={{ name: m.newWorkspaceName, url: m.newWorkspaceSource }}
          inline
        />
      </form>
    </Dialog>
  );
}

function PanelSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="panel-section" aria-label={title}>
      <h3>{title}</h3>
      {children}
    </section>
  );
}

function StartServer({ workspace, onStarted }: { workspace: number; onStarted: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  async function start() {
    setBusy(true);
    setError(null);
    try {
      await api.post("/api/servers", { workspace });
    } catch (e) {
      setError(asApiError(e));
    } finally {
      setBusy(false);
    }
    onStarted();
  }

  return (
    <>
      <button type="button" className="primary" disabled={busy} onClick={start}>
        {m.start}
      </button>
      <ProblemView error={error} />
    </>
  );
}

// What git says a ready workspace holds that deleting it would lose: asked for as the dialog
// opens, null while it is asked; none where the workspace holds no repository.
type UnsavedCheck = { unsaved: Unsaved | null; error: ApiError | null; answered: boolean };

function useUnsaved(workspace: Workspace): UnsavedCheck {
  const ready = workspace.state === "ready";
  const [check, setCheck] = useState<UnsavedCheck>({
    unsaved: null,
    error: null,
    answered: !ready,
  });
  useEffect(() => {
    if (!ready) return;
    let current = true;
    api.get<GitStatus>("/api/git/status", { workspace: workspace.id }).then(
      (status) => {
        if (current) setCheck({ unsaved: unsavedIn(status), error: null, answered: true });
      },
      (e) => {
        if (!current) return;
        const error = asApiError(e);
        // no repository: nothing in git to lose
        if (error.code === "not_a_repository")
          setCheck({ unsaved: null, error: null, answered: true });
        else setCheck({ unsaved: null, error, answered: true });
      },
    );
    return () => {
      current = false;
    };
  }, [ready, workspace.id]);
  return check;
}

// Asks before deleting a workspace, which takes everything in it and its server, naming what
// git says would be lost; Delete waits for git's answer, and a check that failed is said.
function DeleteWorkspaceDialog({
  workspace,
  onDeleted,
  onFailed,
  onCancel,
}: {
  workspace: Workspace;
  onDeleted: () => void;
  onFailed: () => void;
  onCancel: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const { unsaved, error: unchecked, answered } = useUnsaved(workspace);

  async function remove(event: FormEvent) {
    event.preventDefault();
    if (busy || !answered) return;
    setBusy(true);
    setError(null);
    try {
      await api.delete(`/api/workspaces/${workspace.id}`);
      onDeleted();
    } catch (e) {
      setError(asApiError(e));
      setBusy(false);
      onFailed();
    }
  }

  return (
    <Dialog
      owner="DeleteWorkspaceDialog"
      title={m.deleteWorkspaceTitle(workspace.name)}
      onClose={onCancel}
      closable={!busy}
    >
      <form noValidate onSubmit={remove}>
        <p>{m.deleteWorkspaceBody}</p>
        {!answered && <p className="muted">{m.deleteWorkspaceChecking}</p>}
        {unsaved && unsaved.files > 0 && <p>{m.deleteWorkspaceUncommitted(unsaved.files)}</p>}
        {unsaved && unsaved.commits > 0 && <p>{m.deleteWorkspaceUnpushed(unsaved.commits)}</p>}
        {unchecked && (
          <>
            <p className="warning">{m.deleteWorkspaceUnchecked}</p>
            <ProblemView error={unchecked} />
          </>
        )}
        <div className="actions">
          <button type="submit" className="danger" disabled={busy || !answered}>
            {busy ? m.deleting : m.deleteWorkspaceConfirm}
          </button>
          <button type="button" disabled={busy} onClick={onCancel}>
            {m.cancel}
          </button>
        </div>
      </form>
      <ProblemView error={error} />
    </Dialog>
  );
}

// Says why a workspace cannot be deleted now, and asks nothing of the server.
function CannotDeleteDialog({
  workspace,
  reason,
  onClose,
}: {
  workspace: Workspace;
  reason: string;
  onClose: () => void;
}) {
  function dismiss(event: FormEvent) {
    event.preventDefault();
    onClose();
  }
  return (
    <Dialog
      owner="CannotDeleteDialog"
      title={m.cannotDeleteTitle(workspace.name)}
      onClose={onClose}
    >
      <form noValidate onSubmit={dismiss}>
        <p>{reason}</p>
        <div className="actions">
          <button type="submit" className="primary">
            {m.close}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

// Why a workspace failed: a sentence in the open, and what the server said, kept behind Details.
function FailureSaid({ error, cloned }: { error: string | null; cloned: boolean }) {
  const { sentence, raw } = failureSentence(error, cloned);
  return (
    <>
      <p>{sentence}</p>
      {raw && (
        <details>
          <summary>{m.detailsTitle}</summary>
          <pre className="screen workspace-error">{error}</pre>
        </details>
      )}
    </>
  );
}

// The steps taken while a workspace is made, named where they are seen: a figure's name is not
// shown by itself, and not every reader takes it from its caption.
function Steps({ log }: { log: string }) {
  const caption = useId();
  return (
    <figure aria-labelledby={caption}>
      <figcaption id={caption}>{m.workspaceLog}</figcaption>
      <pre className="screen workspace-log">{log}</pre>
    </figure>
  );
}

// The tabs of a ready workspace's panel, in order, each by the name the address gives it
// (`?tab=`); the first is the one shown when the address names none it knows.
const TABS = [
  { name: "overview", label: m.tabOverview },
  { name: "terminal", label: m.tabTerminal },
] as const;
type TabName = (typeof TABS)[number]["name"];

// Which tab the address shows, and the choice of another, kept in the address in place of
// the one before it.
function useTab(): [TabName, (tab: TabName) => void] {
  const [params, setParams] = useSearchParams();
  const asked = params.get("tab");
  const tab = TABS.find((t) => t.name === asked)?.name ?? TABS[0].name;
  const choose = (next: TabName) =>
    setParams(
      (current) => {
        const params = new URLSearchParams(current);
        if (next === TABS[0].name) params.delete("tab");
        else params.set("tab", next);
        return params;
      },
      { replace: true },
    );
  return [tab, choose];
}

// The tab list: choosing a tab by pressing it, or moving between them with the arrow keys,
// Home and End, which choose the tab moved to.
function TabList({
  id,
  tab,
  onChoose,
}: {
  id: string;
  tab: TabName;
  onChoose: (tab: TabName) => void;
}) {
  const list = useRef<HTMLDivElement>(null);
  function moved(event: KeyboardEvent) {
    const at = TABS.findIndex((t) => t.name === tab);
    const to = {
      ArrowRight: (at + 1) % TABS.length,
      ArrowLeft: (at - 1 + TABS.length) % TABS.length,
      Home: 0,
      End: TABS.length - 1,
    }[event.key];
    if (to === undefined) return;
    event.preventDefault();
    onChoose(TABS[to].name);
    list.current
      ?.querySelector<HTMLElement>(`#${CSS.escape(`${id}-tab-${TABS[to].name}`)}`)
      ?.focus();
  }
  return (
    <div role="tablist" aria-label={m.workspaceTabs} className="tabs" ref={list} onKeyDown={moved}>
      {TABS.map((t) => (
        <button
          key={t.name}
          type="button"
          role="tab"
          id={`${id}-tab-${t.name}`}
          aria-selected={t.name === tab}
          aria-controls={`${id}-panel-${t.name}`}
          tabIndex={t.name === tab ? 0 : -1}
          onClick={() => onChoose(t.name)}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}

// A tab's panel, kept on the page while another tab is shown.
function TabPanel({
  id,
  name,
  shown,
  children,
}: {
  id: string;
  name: TabName;
  shown: boolean;
  children: ReactNode;
}) {
  return (
    <div
      role="tabpanel"
      id={`${id}-panel-${name}`}
      aria-labelledby={`${id}-tab-${name}`}
      hidden={!shown}
      // biome-ignore lint/a11y/noNoninteractiveTabindex: the WAI-ARIA tab pattern has a tab panel take focus, so that Tab reaches it from its tab whatever it holds first
      tabIndex={0}
    >
      {children}
    </div>
  );
}

// The selected workspace: under its name, the GitHub page of the repository it was
// cloned from, when the server gives one; what its state allows, and, once it is ready,
// two tabs: Overview, with its Claude server or the offer to start one and the Changes of
// its repository, and Terminal, a shell in it, opened once the tab is first shown. Each
// tab's panel stays on the page while the other is shown, so the terminal stays open.
function WorkspacePanel({
  workspace,
  approved,
  pollMs,
  onChange,
}: {
  workspace: Workspace;
  approved: string | null;
  pollMs: number;
  onChange: () => void;
}) {
  const [tab, chooseTab] = useTab();
  const ready = workspace.state === "ready";
  // The terminal is opened when its tab is first shown, and kept from then on.
  const [terminalOpened, setTerminalOpened] = useState(false);
  const terminalShown = ready && tab === "terminal";
  useEffect(() => {
    if (terminalShown) setTerminalOpened(true);
  }, [terminalShown]);
  const tabsId = useId();
  const cloning = workspace.state === "creating" && workspace.source !== null;
  const { server, server_error: unread } = workspace;
  return (
    <section aria-label={workspace.name} className="panel">
      <h2>{workspace.name}</h2>
      {workspace.source_page && (
        <p className="source-page">
          <a href={workspace.source_page} target="_blank" rel="noopener noreferrer">
            {workspace.source_page}
          </a>
        </p>
      )}
      {workspace.state === "creating" && <p>{m.workspaceCreating}</p>}
      {cloning && <p>{m.workspaceCloningCancel}</p>}
      {workspace.state === "deleting" && <p>{m.workspaceDeleting}</p>}
      {workspace.state === "failed" && (
        <>
          <p>{m.workspaceFailed}</p>
          <FailureSaid error={workspace.error} cloned={workspace.source !== null} />
        </>
      )}
      {!ready && workspace.log && <Steps log={workspace.log} />}
      {ready && (
        <>
          <TabList id={tabsId} tab={tab} onChoose={chooseTab} />
          <TabPanel id={tabsId} name="overview" shown={tab === "overview"}>
            <PanelSection title={m.serverTitle}>
              {unread && (
                <>
                  <ProblemView error={new ApiError(0, unread.code, unread.title)} />
                  <p>{m.serverUnreadNextStep}</p>
                </>
              )}
              {server && (
                <ServerCard
                  workspace={workspace}
                  server={server}
                  claudeVersion={workspace.claude_version}
                  onChange={onChange}
                />
              )}
              {server &&
                // only a server that runs can be stopped and started again to take the new version
                ACTIONS[server.state]?.includes("stop") &&
                workspace.claude_version &&
                approved &&
                workspace.claude_version !== approved && (
                  <p className="muted">{m.serverVersionDiffers(approved)}</p>
                )}
              {!server && !unread && (
                <>
                  <p>{m.noServer}</p>
                  <StartServer workspace={workspace.id} onStarted={onChange} />
                </>
              )}
            </PanelSection>
            <Changes workspace={workspace.id} pollMs={pollMs} />
          </TabPanel>
          <TabPanel id={tabsId} name="terminal" shown={terminalShown}>
            {(terminalOpened || terminalShown) && (
              <Terminal workspace={workspace.id} shown={terminalShown} onFailure={onChange} />
            )}
          </TabPanel>
        </>
      )}
    </section>
  );
}

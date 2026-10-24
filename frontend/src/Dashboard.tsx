import {
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
  type RefObject,
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
import { BackIcon, PlusIcon, RepoIcon, TrashIcon, WorkspacesIcon } from "./Icons";
import { MessageBar } from "./MessageBar";
import { m } from "./messages";
import { asApiError, ProblemView, useFieldErrors } from "./ProblemView";
import {
  ACTIONS,
  dotFor,
  REBUILDING,
  ServerCard,
  StatusDot,
  type Workspace,
  WorkspaceBadge,
} from "./Servers";
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

// The repository a workspace's page names, owner first, as the panel's head shows it: the page's
// path, or the page whole where it is no address.
function repositoryOf(page: string): string {
  try {
    return new URL(page).pathname.replace(/^\/+|\/+$/g, "") || page;
  } catch {
    return page;
  }
}

// New workspace: the page's one primary action, beside its heading or in the empty list; while
// rcman is being upgraded, which makes none meanwhile, not offered, and described by why
// (``why``, the id of what says so).
function NewWorkspaceButton({ onClick, why }: { onClick: () => void; why: string | null }) {
  return (
    // named in full wherever a phone shows its short words alone
    <button
      type="button"
      className="primary"
      aria-label={m.newWorkspace}
      onClick={onClick}
      disabled={why !== null}
      aria-describedby={why ?? undefined}
    >
      <PlusIcon />
      <span className="label-long">{m.newWorkspace}</span>
      <span className="label-short" aria-hidden="true">
        {m.newWorkspaceShort}
      </span>
    </button>
  );
}

// The Workspaces page: under its heading, with New workspace beside it, the person's
// workspaces, each a row with its trash button, and the selected workspace's panel beside them.
// The selected workspace is in the address. With no workspaces, the list is an empty state that
// offers New workspace in the heading's place. On a phone the list and the panel take turns:
// with a workspace selected, the panel, under a bar with the way back to the list and the
// switcher to another.
export function Dashboard({ pollMs }: { pollMs: number }) {
  const [workspaces, setWorkspaces] = useState<Workspace[] | null>(null);
  // The Claude Code version a server started now would run, null where none is approved.
  const [approved, setApproved] = useState<string | null>(null);
  // Whether rcman is being upgraded, while which no workspace is made.
  const [upgrading, setUpgrading] = useState(false);
  const upgradingNote = useId();
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
      const answer = await api.get<{
        workspaces: Workspace[];
        claude_approved: string | null;
        rcman_upgrading?: boolean;
      }>("/api/workspaces");
      if (mine === latest.current) {
        setWorkspaces(answer.workspaces);
        setApproved(answer.claude_approved);
        setUpgrading(answer.rcman_upgrading === true);
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

  // A workspace as an answer gave it, in the list's place of the one it was, until the next
  // refresh, which is asked for at once.
  function updated(workspace: Workspace) {
    setWorkspaces(
      (current) => current?.map((w) => (w.id === workspace.id ? workspace : w)) ?? null,
    );
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
  const empty = workspaces?.length === 0;
  let shown: ReactNode;
  if (asked === null) shown = m.workspacePanelEmpty;
  else if (workspaces === null) shown = m.loading;
  else if (!selected) shown = m.workspaceUnknown;
  const classes = ["dashboard"];
  if (asked !== null) classes.push("showing");
  if (empty && asked === null) classes.push("empty");

  return (
    <>
      <div className={asked === null ? "page-head" : "page-head showing"}>
        <h1 id={heading} tabIndex={-1} ref={headingRef}>
          {m.navDashboard}
        </h1>
        {/* how many there are, which a phone shows in the heading's place */}
        {workspaces && !empty && (
          <p className="count-line muted">{m.workspaceCount(workspaces.length)}</p>
        )}
        {/* in the empty list's place when there are none; not before the list is known */}
        {(workspaces ? !empty : error !== null) && (
          <NewWorkspaceButton
            onClick={() => setMaking(true)}
            why={upgrading ? upgradingNote : null}
          />
        )}
      </div>
      <ProblemView error={error} />
      {upgrading && (
        <MessageBar kind="info">
          <p id={upgradingNote}>{m.workspacesUpgrading}</p>
        </MessageBar>
      )}
      {notice && (
        <MessageBar kind="success" role="status">
          <p>{notice}</p>
        </MessageBar>
      )}
      <div className={classes.join(" ")}>
        <WorkspaceList
          labelledBy={heading}
          workspaces={workspaces}
          selected={selectedId}
          onSelect={select}
          onRemove={setRemoving}
          onMake={() => setMaking(true)}
          why={upgrading ? upgradingNote : null}
        />
        {!(empty && asked === null) && (
          <div className="panel-side">
            {asked !== null && (
              <PanelBar workspaces={workspaces ?? []} selected={selectedId} onSelect={select} />
            )}
            {selected ? (
              <WorkspacePanel
                key={selected.id}
                workspace={selected}
                approved={approved}
                upgrading={upgrading}
                pollMs={pollMs}
                onChange={refresh}
                onUpdated={updated}
              />
            ) : (
              <section className="panel">
                <p className="card placeholder">{shown}</p>
              </section>
            )}
          </div>
        )}
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

// Each workspace, a row selecting it, with its dot before its name and, while it is not ready,
// its state, and its trash button, named after it; the list is named by the page's heading.
// With none, the list is an empty state offering New workspace.
function WorkspaceList({
  labelledBy,
  workspaces,
  selected,
  onSelect,
  onRemove,
  onMake,
  why,
}: {
  labelledBy: string;
  workspaces: Workspace[] | null;
  selected: number | null;
  onSelect: (id: number) => void;
  onRemove: (workspace: Workspace) => void;
  onMake: () => void;
  why: string | null;
}) {
  return (
    <section aria-labelledby={labelledBy} className="list card">
      {workspaces?.length === 0 && (
        <div className="empty-state">
          <span className="empty-icon">
            <WorkspacesIcon />
          </span>
          <strong>{m.workspacesEmpty}</strong>
          <p>{m.workspacesEmptyBody}</p>
          <NewWorkspaceButton onClick={onMake} why={why} />
        </div>
      )}
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
  const deviceId = useId();
  const dot = dotFor(workspace);
  return (
    <li className={current ? "list-row selected" : "list-row"}>
      <button
        type="button"
        className="name"
        aria-current={current ? "true" : undefined}
        aria-describedby={dot ? `${id} ${deviceId}` : deviceId}
        onClick={() => onSelect(workspace.id)}
      >
        {/* the dot's room is kept where there is none, so that the names line up */}
        <span className="dot-slot">{dot && <StatusDot kind={dot} id={id} />}</span>
        <WorkspaceNames workspace={workspace} deviceId={deviceId} />
      </button>
      {workspace.state !== "ready" && <WorkspaceBadge state={workspace.state} />}
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

// A workspace's name over its device name, the one claude.ai and the Claude app give its server,
// small and with no label. The device name is left out of the button's name, which is the
// workspace's, and read as its description instead (deviceId).
function WorkspaceNames({ workspace, deviceId }: { workspace: Workspace; deviceId: string }) {
  return (
    <span className="names">
      <span className="name-text">{workspace.name}</span>
      <code className="device muted" id={deviceId} translate="no" aria-hidden="true">
        {workspace.device}
      </code>
    </span>
  );
}

// Over the panel, where the list is not beside it, as on a phone: one bar of the way back to the
// list and the switcher, which shows the workspace's name and opens the workspaces as a sheet
// from the screen's foot, focus on the one shown. The stylesheet shows it only there.
function PanelBar({
  workspaces,
  selected,
  onSelect,
}: {
  workspaces: Workspace[];
  selected: number | null;
  onSelect: (id: number) => void;
}) {
  const sheet = usePopup("PanelBar");
  const list = useRef<HTMLUListElement>(null);
  const name = workspaces.find((w) => w.id === selected)?.name ?? "";
  useEffect(() => {
    if (sheet.open) list.current?.querySelector<HTMLElement>('[aria-current="true"]')?.focus();
  }, [sheet.open]);
  return (
    <div className="panel-bar-root" ref={sheet.rootRef}>
      <div className="panel-bar">
        <Link
          to={workspaceLink(null)}
          className="button subtle icon-button"
          aria-label={m.backToWorkspaces}
          title={m.backToWorkspaces}
        >
          <BackIcon />
        </Link>
        <button
          type="button"
          className="subtle switcher"
          aria-label={m.switchWorkspaceFrom(name)}
          {...sheet.buttonProps}
        >
          <span className="name-text">{name}</span>
          <Caret />
        </button>
      </div>
      {/* the page dimmed above the sheet, which takes a press and closes it */}
      <div
        className="scrim"
        aria-hidden="true"
        hidden={!sheet.open}
        // a press keeps focus where it is, so that it goes back to the switcher as the sheet closes
        onMouseDown={(event) => event.preventDefault()}
        onClick={() => sheet.close(true)}
      />
      <div className="sheet" {...sheet.panelProps} hidden={!sheet.open}>
        <p className="sheet-title" id={`${sheet.panelProps.id}-title`}>
          {m.switchWorkspace}
        </p>
        <ul ref={list} aria-labelledby={`${sheet.panelProps.id}-title`}>
          {workspaces.map((workspace) => (
            <li
              key={workspace.id}
              className={workspace.id === selected ? "list-row selected" : "list-row"}
            >
              <button
                type="button"
                className="name"
                aria-current={workspace.id === selected ? "true" : undefined}
                aria-describedby={`${sheet.panelProps.id}-device-${workspace.id}`}
                onClick={() => {
                  sheet.close(true);
                  onSelect(workspace.id);
                }}
              >
                <WorkspaceNames
                  workspace={workspace}
                  deviceId={`${sheet.panelProps.id}-device-${workspace.id}`}
                />
                {workspace.state !== "ready" && (
                  <>
                    {" "}
                    <WorkspaceBadge state={workspace.state} />
                  </>
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
        <p id={nameHelp} className="muted help">
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
        <p id={urlHelp} className="muted help">
          {m.newWorkspaceSourceHelp}
        </p>
        <ProblemView
          error={error}
          labels={{ name: m.newWorkspaceName, url: m.newWorkspaceSource }}
          inline
        />
        <div className="actions footer">
          <button type="submit" className="primary" disabled={busy}>
            {busy ? m.creating : m.create}
          </button>
          <button type="button" disabled={busy} onClick={onCancel}>
            {m.cancel}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

// One of Overview's cards, named by its heading, which can be given focus (headingRef).
function PanelSection({
  title,
  headingRef,
  children,
}: {
  title: string;
  headingRef?: RefObject<HTMLHeadingElement | null>;
  children: ReactNode;
}) {
  return (
    <section className="card panel-section" aria-label={title}>
      <h3 ref={headingRef} tabIndex={headingRef ? -1 : undefined}>
        {title}
      </h3>
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
      <div className="actions">
        <button type="button" className="primary" disabled={busy} onClick={start}>
          {m.start}
        </button>
      </div>
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
            <MessageBar kind="warning">
              <p>{m.deleteWorkspaceUnchecked}</p>
            </MessageBar>
            <ProblemView error={unchecked} />
          </>
        )}
        <ProblemView error={error} />
        <div className="actions footer">
          <button type="submit" className="danger" disabled={busy || !answered}>
            {busy ? m.deleting : m.deleteWorkspaceConfirm}
          </button>
          <button type="button" disabled={busy} onClick={onCancel}>
            {m.cancel}
          </button>
        </div>
      </form>
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
        <div className="actions footer">
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
      <p>{m.workspaceFailed}</p>
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

// The steps taken while a workspace is made or rebuilt, named where they are seen: a figure's
// name is not shown by itself, and not every reader takes it from its caption. `live` while they
// are still being taken, so that a new step is announced, politely.
function Steps({ log, live = false }: { log: string; live?: boolean }) {
  const caption = useId();
  return (
    <figure aria-labelledby={caption}>
      <figcaption id={caption}>{m.workspaceLog}</figcaption>
      <pre className="screen workspace-log" aria-live={live ? "polite" : undefined}>
        {log}
      </pre>
    </figure>
  );
}

// No rebuild under way, and none recorded.
const NO_REBUILD = { state: null, log: null, error: null };
const rebuildOf = (workspace: Workspace) => workspace.rebuild ?? NO_REBUILD;

// Why a rebuild did not finish, as the server says it: its first line, which says what became of
// the workspace and what to do, in the open; the rest, why it failed, behind Details.
function RebuildFailed({ error }: { error: string }) {
  const [first, ...rest] = error.trim().split("\n");
  return (
    <MessageBar kind="problem" role="alert">
      <p>{m.rebuildFailed}</p>
      <p>{first}</p>
      {rest.length > 0 && (
        <details>
          <summary>{m.detailsTitle}</summary>
          <pre className="screen workspace-error">{rest.join("\n")}</pre>
        </details>
      )}
    </MessageBar>
  );
}

// Rebuild, offered where the server says a rebuild may be asked for now (`rebuildable`) and rcman
// is not being upgraded, and shown waiting while one is under way; it asks first. `primary` where
// it is the thing to do next. Once one is started, focus goes to `focusAfter`, since the button
// waits from then on.
function RebuildAction({
  workspace,
  upgrading,
  primary,
  focusAfter,
  onStarted,
  onFailed,
}: {
  workspace: Workspace;
  upgrading: boolean;
  primary: boolean;
  focusAfter: RefObject<HTMLElement | null>;
  onStarted: (workspace: Workspace) => void;
  onFailed: () => void;
}) {
  const [asking, setAsking] = useState(false);
  // Set once a rebuild is started: focus moves once the dialog has given it back.
  const refocus = useRef(false);
  useEffect(() => {
    if (asking || !refocus.current) return;
    refocus.current = false;
    focusAfter.current?.focus();
  });
  const running = rebuildOf(workspace).state !== null;
  if (!running && (!workspace.rebuildable || upgrading)) return null;
  return (
    <>
      <div className="actions">
        <button
          type="button"
          className={primary && !running ? "primary" : undefined}
          disabled={running}
          onClick={() => setAsking(true)}
        >
          {running ? m.rebuildBusy : m.rebuild}
        </button>
      </div>
      {asking && (
        <RebuildWorkspaceDialog
          workspace={workspace}
          onStarted={(started) => {
            refocus.current = true;
            setAsking(false);
            onStarted(started);
          }}
          onFailed={onFailed}
          onCancel={() => setAsking(false)}
        />
      )}
    </>
  );
}

// Asks before a rebuild: what it does, what it ends and what it keeps, that the repository's
// commands run again, and what the build may cost; for a failed workspace, that there is nothing
// earlier to go back to. A refusal is shown in it.
function RebuildWorkspaceDialog({
  workspace,
  onStarted,
  onFailed,
  onCancel,
}: {
  workspace: Workspace;
  onStarted: (workspace: Workspace) => void;
  onFailed: () => void;
  onCancel: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const failed = workspace.state === "failed";

  async function rebuild(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const answer = await api.post<{ workspace: Workspace }>(
        `/api/workspaces/${workspace.id}/rebuild`,
        { confirm: true },
      );
      onStarted(answer.workspace);
    } catch (e) {
      setError(asApiError(e));
      setBusy(false);
      onFailed();
    }
  }

  return (
    <Dialog
      owner="RebuildWorkspaceDialog"
      title={m.rebuildTitle(workspace.name)}
      onClose={onCancel}
      closable={!busy}
    >
      <form noValidate onSubmit={rebuild}>
        <p>{failed ? m.rebuildIntroFailed : m.rebuildIntro}</p>
        {!failed && (
          <>
            <p>{m.rebuildEndsTitle}</p>
            <ul className="rebuild-list">
              {m.rebuildEnds.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          </>
        )}
        <p>{m.rebuildKeepsTitle}</p>
        <ul className="rebuild-list">
          {m.rebuildKeeps.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
        <p>{failed ? m.rebuildCommandsFailed : m.rebuildCommands}</p>
        <p>{m.rebuildMemory}</p>
        <ProblemView error={error} />
        <div className="actions footer">
          <button type="submit" className="primary" disabled={busy}>
            {busy ? m.rebuildBusy : m.rebuildConfirm}
          </button>
          <button type="button" disabled={busy} onClick={onCancel}>
            {m.cancel}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

// Whether the workspace's devcontainer.json, or its Dockerfile, changed since its container was
// made: asked when the card opens and every pollMs; null where the server does not know, and
// where it could not be asked, which the card does not remark on. An answer older than one asked
// for since is dropped.
function useDevcontainerChanged(workspace: number, pollMs: number): boolean | null {
  const [changed, setChanged] = useState<boolean | null>(null);
  const latest = useRef(0);
  useEffect(() => {
    const ask = async () => {
      const mine = ++latest.current;
      let answer: boolean | null = null;
      try {
        const said = await api.get<{ changed: boolean | null }>(
          `/api/workspaces/${workspace}/devcontainer`,
        );
        answer = said.changed;
      } catch {
        answer = null;
      }
      if (mine === latest.current) setChanged(answer);
    };
    ask();
    const timer = pollMs > 0 ? setInterval(ask, pollMs) : undefined;
    return () => {
      clearInterval(timer);
      latest.current++;
    };
  }, [workspace, pollMs]);
  return changed;
}

// The Container card of a ready workspace's Overview: what its container is made from, whether
// its devcontainer.json changed since, Rebuild, and how a rebuild goes or why the last one did
// not finish.
function ContainerCard({
  workspace,
  upgrading,
  pollMs,
  onUpdated,
  onChange,
}: {
  workspace: Workspace;
  upgrading: boolean;
  pollMs: number;
  onUpdated: (workspace: Workspace) => void;
  onChange: () => void;
}) {
  const changed = useDevcontainerChanged(workspace.id, pollMs);
  const heading = useRef<HTMLHeadingElement>(null);
  const rebuild = rebuildOf(workspace);
  const running = rebuild.state !== null;
  return (
    <PanelSection title={m.containerTitle} headingRef={heading}>
      <p className="muted">{workspace.source === null ? m.containerEmpty : m.containerIntro}</p>
      {changed === true && !running && (
        <MessageBar kind="info">
          <p>{m.devcontainerChanged}</p>
        </MessageBar>
      )}
      {running && (
        <MessageBar kind="info">
          <p>{m.rebuildRunning}</p>
        </MessageBar>
      )}
      {!running && rebuild.error && <RebuildFailed error={rebuild.error} />}
      {rebuild.log && <Steps log={rebuild.log} live={running} />}
      <RebuildAction
        workspace={workspace}
        upgrading={upgrading}
        primary={changed === true}
        focusAfter={heading}
        onStarted={onUpdated}
        onFailed={onChange}
      />
    </PanelSection>
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
  upgrading,
  pollMs,
  onChange,
  onUpdated,
}: {
  workspace: Workspace;
  approved: string | null;
  upgrading: boolean;
  pollMs: number;
  onChange: () => void;
  onUpdated: (workspace: Workspace) => void;
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
  const { server, server_error: serverError } = workspace;
  // While a rebuild replaces its container, the server is not read: that is said, not a problem.
  const replacing = serverError?.code === REBUILDING;
  const unread = replacing ? null : serverError;
  const rebuild = rebuildOf(workspace);
  const failedRebuilding = workspace.state === "failed" && rebuild.state !== null;
  // A failed workspace's steps are its last rebuild's where it had one, else its making's.
  const steps = workspace.state === "failed" ? (rebuild.log ?? workspace.log) : workspace.log;
  const title = useRef<HTMLHeadingElement>(null);
  return (
    <section aria-label={workspace.name} className="panel">
      <div className="panel-head">
        <div className="panel-title">
          <h2 ref={title} tabIndex={-1}>
            {workspace.name}
          </h2>
          <WorkspaceBadge state={workspace.state} />
        </div>
        {workspace.source_page && (
          <a
            className="repo-link"
            href={workspace.source_page}
            target="_blank"
            rel="noopener noreferrer"
            aria-label={workspace.source_page}
            title={workspace.source_page}
          >
            <RepoIcon />
            <span>{repositoryOf(workspace.source_page)}</span>
          </a>
        )}
        <p className="panel-device muted">
          {/* a space that does not break, so that the label stays with the name */}
          {m.device}
          {"\u00a0"}
          <code translate="no">{workspace.device}</code>
        </p>
      </div>
      {workspace.state === "creating" && (
        <MessageBar kind="info">
          <p>{m.workspaceCreating}</p>
          {cloning && <p>{m.workspaceCloningCancel}</p>}
        </MessageBar>
      )}
      {workspace.state === "deleting" && (
        <MessageBar kind="info">
          <p>{m.workspaceDeleting}</p>
        </MessageBar>
      )}
      {workspace.state === "failed" &&
        (failedRebuilding ? (
          <MessageBar kind="info">
            <p>{m.rebuildRunning}</p>
          </MessageBar>
        ) : (
          <MessageBar kind="problem">
            <FailureSaid error={workspace.error} cloned={workspace.source !== null} />
          </MessageBar>
        ))}
      {workspace.state === "failed" && (
        <RebuildAction
          workspace={workspace}
          upgrading={upgrading}
          primary
          focusAfter={title}
          onStarted={onUpdated}
          onFailed={onChange}
        />
      )}
      {!ready && steps && <Steps log={steps} live={failedRebuilding} />}
      {ready && (
        <>
          <TabList id={tabsId} tab={tab} onChoose={chooseTab} />
          <TabPanel id={tabsId} name="overview" shown={tab === "overview"}>
            <div className="overview">
              <PanelSection title={m.serverTitle}>
                {replacing && <p>{m.serverRebuilding}</p>}
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
                {!server && !serverError && (
                  <>
                    <p>{m.noServer}</p>
                    <StartServer workspace={workspace.id} onStarted={onChange} />
                  </>
                )}
              </PanelSection>
              <ContainerCard
                workspace={workspace}
                upgrading={upgrading}
                pollMs={pollMs}
                onUpdated={onUpdated}
                onChange={onChange}
              />
              <Changes workspace={workspace.id} pollMs={pollMs} />
            </div>
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

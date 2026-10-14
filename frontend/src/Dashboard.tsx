import {
  type FormEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { ApiError, api } from "./api";
import { m } from "./messages";
import { asApiError, ProblemView } from "./ProblemView";
import { dotFor, ServerCard, StatusDot, type Workspace } from "./Servers";

// Where a workspace's panel is: the Workspaces page, with the workspace in the address.
const workspaceLink = (id: number | null) => ({
  pathname: "/",
  search: id === null ? "" : `?workspace=${id}`,
});

// The Workspaces page: the person's workspaces and the form that makes one on
// one side, and the selected workspace's panel on the other. The selected
// workspace is in the address.
export function Dashboard({ pollMs }: { pollMs: number }) {
  const [workspaces, setWorkspaces] = useState<Workspace[] | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  // What was done, shown while nothing is selected after it.
  const [notice, setNotice] = useState<string | null>(null);
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const asked = params.get("workspace");
  const selectedId = asked && /^\d+$/.test(asked) ? Number(asked) : null;

  // A poll's failure is shown until a later poll works; an answer older than one
  // asked for since is dropped.
  const latest = useRef(0);
  const refresh = useCallback(async () => {
    const mine = ++latest.current;
    try {
      const answer = await api.get<{ workspaces: Workspace[] }>("/api/workspaces");
      if (mine === latest.current) {
        setWorkspaces(answer.workspaces);
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
    setWorkspaces((current) =>
      current && !current.some((w) => w.id === workspace.id) ? [...current, workspace] : current,
    );
    navigate(workspaceLink(workspace.id));
    refresh();
  }

  function deleted(workspace: Workspace) {
    setWorkspaces((current) => current?.filter((w) => w.id !== workspace.id) ?? null);
    navigate(workspaceLink(null), { replace: true });
    setNotice(m.workspaceDeleted(workspace.name));
    refresh();
  }

  const selected = workspaces?.find((w) => w.id === selectedId);
  let shown: ReactNode;
  if (asked === null) shown = <p>{m.workspacePanelEmpty}</p>;
  else if (workspaces === null) shown = <p>{m.loading}</p>;
  else if (!selected) shown = <p>{m.workspaceUnknown}</p>;

  return (
    <>
      <ProblemView error={error} />
      {notice && asked === null && <p role="status">{notice}</p>}
      <div className="dashboard">
        <div className="pane">
          <WorkspaceList workspaces={workspaces} selected={selectedId} onSelect={select} />
          <NewWorkspace onMade={made} />
        </div>
        {selected ? (
          <WorkspacePanel
            key={selected.id}
            workspace={selected}
            onChange={refresh}
            onDeleted={() => deleted(selected)}
          />
        ) : (
          <section className="panel">{shown}</section>
        )}
      </div>
    </>
  );
}

// Each workspace, selecting it, with its dot and, while it is not ready, its state.
function WorkspaceList({
  workspaces,
  selected,
  onSelect,
}: {
  workspaces: Workspace[] | null;
  selected: number | null;
  onSelect: (id: number) => void;
}) {
  return (
    <section aria-label={m.workspacesTitle}>
      <h2>{m.workspacesTitle}</h2>
      {workspaces?.length === 0 && <p>{m.workspacesEmpty}</p>}
      {workspaces && workspaces.length > 0 && (
        <ul className="workspaces">
          {workspaces.map((workspace) => (
            <WorkspaceEntry
              key={workspace.id}
              workspace={workspace}
              current={workspace.id === selected}
              onSelect={onSelect}
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
}: {
  workspace: Workspace;
  current: boolean;
  onSelect: (id: number) => void;
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
    </li>
  );
}

// Makes a workspace with the name typed, which is the server's to judge.
function NewWorkspace({ onMade }: { onMade: (workspace: Workspace) => void }) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  async function create(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const answer = await api.post<{ workspace: Workspace }>("/api/workspaces", { name });
      setName("");
      onMade(answer.workspace);
    } catch (e) {
      setError(asApiError(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-label={m.newWorkspace}>
      <h2>{m.newWorkspace}</h2>
      <form noValidate onSubmit={create}>
        <label>
          {m.newWorkspaceName}
          <input type="text" name="name" value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <div className="actions">
          <button type="submit" className="primary" disabled={busy}>
            {busy ? m.creating : m.create}
          </button>
        </div>
        <ProblemView error={error} labels={{ name: m.newWorkspaceName }} />
      </form>
    </section>
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

// Asks before deleting a workspace, which takes everything in it and its server.
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

  async function remove(event: FormEvent) {
    event.preventDefault();
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
    <div role="dialog" aria-label={m.deleteWorkspaceTitle(workspace.name)} className="dialog">
      <h3>{m.deleteWorkspaceTitle(workspace.name)}</h3>
      <form noValidate onSubmit={remove}>
        <p>{m.deleteWorkspaceBody}</p>
        <div className="actions">
          <button type="submit" className="danger" disabled={busy}>
            {busy ? m.deleting : m.deleteWorkspaceConfirm}
          </button>
          <button type="button" disabled={busy} onClick={onCancel}>
            {m.cancel}
          </button>
        </div>
      </form>
      <ProblemView error={error} />
    </div>
  );
}

// The selected workspace: what its state allows, and, once it is ready, its
// Claude server or the offer to start one. Delete workspace is offered for a
// workspace that is ready or failed.
function WorkspacePanel({
  workspace,
  onChange,
  onDeleted,
}: {
  workspace: Workspace;
  onChange: () => void;
  onDeleted: () => void;
}) {
  const [deleting, setDeleting] = useState(false);
  const deletable = workspace.state === "ready" || workspace.state === "failed";
  const { server, server_error: unread } = workspace;
  return (
    <section aria-label={workspace.name} className="panel">
      <h2>{workspace.name}</h2>
      {deletable && (
        <>
          {/* biome-ignore lint/a11y/useSemanticElements: a row of buttons outside any form, not a form's fields, which a fieldset is for */}
          <div role="group" aria-label={m.workspaceActions} className="actions">
            <button type="button" className="secondary-danger" onClick={() => setDeleting(true)}>
              {m.deleteWorkspace}
            </button>
          </div>
          {deleting && (
            <DeleteWorkspaceDialog
              workspace={workspace}
              onDeleted={onDeleted}
              onFailed={onChange}
              onCancel={() => setDeleting(false)}
            />
          )}
        </>
      )}
      {workspace.state === "creating" && <p>{m.workspaceCreating}</p>}
      {workspace.state === "deleting" && <p>{m.workspaceDeleting}</p>}
      {workspace.state === "failed" && (
        <>
          <p>{m.workspaceFailed}</p>
          <pre className="workspace-error">{workspace.error}</pre>
        </>
      )}
      {workspace.state === "ready" && (
        <PanelSection title={m.serverTitle}>
          {unread && <ProblemView error={new ApiError(0, unread.code, unread.title)} />}
          {server && <ServerCard workspace={workspace} server={server} onChange={onChange} />}
          {!server && !unread && (
            <>
              <p>{m.noServer}</p>
              <StartServer workspace={workspace.id} onStarted={onChange} />
            </>
          )}
        </PanelSection>
      )}
    </section>
  );
}

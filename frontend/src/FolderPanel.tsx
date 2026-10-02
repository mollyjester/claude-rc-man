import { type FormEvent, type ReactNode, useState } from "react";
import { Link } from "react-router-dom";

import { ApiError, api } from "./api";
import { folderLink, lineage } from "./Folders";
import { m } from "./messages";
import { ProblemView, asApiError } from "./ProblemView";
import { type Server, ServerCard, type StartBlocked, serversIn } from "./Servers";

// Home, then each folder down to the selected one. Each step and the slash after
// it are one .path-part, so that the breadcrumb wraps only after a slash; the
// slashes are only for the eye.
function Breadcrumb({ path }: { path: string }) {
  const folders = lineage(path);
  const slash = <span aria-hidden="true">/</span>;
  return (
    <nav aria-label={m.breadcrumb} className="breadcrumb">
      <ol>
        <li>
          <span className="path-part">
            <Link to={folderLink(null)}>{m.home}</Link>
            {folders.length > 0 && slash}
          </span>
        </li>
        {folders.map((folder, i) => (
          <li key={folder.path}>
            <span className="path-part">
              {i < folders.length - 1 ? (
                <>
                  <Link to={folderLink(folder.path)}>{folder.name}</Link>
                  {slash}
                </>
              ) : (
                <span aria-current="page">{folder.name}</span>
              )}
            </span>
          </li>
        ))}
      </ol>
    </nav>
  );
}

function PanelSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="panel-section">
      <h3>{title}</h3>
      {children}
    </section>
  );
}

function StartServer({ path, onStarted }: { path: string; onStarted: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  async function start() {
    setBusy(true);
    setError(null);
    try {
      await api.post("/api/servers", { path });
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

// What the page does once a folder has been made or deleted, or a delete failed.
export type FolderChanges = {
  created: (parent: string, folder: string) => void;
  deleted: (path: string) => void;
  deleteFailed: (path: string) => void;
};

// Makes a folder in parent ("" for the home folder); its name is the server's to judge.
function NewFolderForm({
  parent,
  onCreated,
  onCancel,
}: {
  parent: string;
  onCreated: (folder: string) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  async function create(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onCreated((await api.post<{ created: string }>("/api/folders", { parent, name })).created);
    } catch (e) {
      setError(asApiError(e));
      setBusy(false);
    }
  }

  return (
    <form noValidate onSubmit={create}>
      <label>
        {m.newFolderName}
        <input type="text" value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      <div className="actions">
        <button type="submit" className="primary" disabled={busy}>
          {m.create}
        </button>
        <button type="button" onClick={onCancel}>
          {m.cancel}
        </button>
      </div>
      <ProblemView error={error} labels={{ name: m.newFolderName }} />
    </form>
  );
}

// Asks before deleting a folder, naming it and the servers that are stopped and
// forgotten first.
function DeleteFolderDialog({
  path,
  servers,
  onDeleted,
  onFailed,
  onCancel,
}: {
  path: string;
  servers: Server[];
  onDeleted: () => void;
  onFailed: () => void;
  onCancel: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const inside = serversIn(path, servers);

  async function remove(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.delete("/api/folders", { path });
      onDeleted();
    } catch (e) {
      setError(asApiError(e));
      setBusy(false);
      onFailed();
    }
  }

  return (
    <div role="dialog" aria-label={m.deleteFolderTitle(path)} className="dialog">
      <h3>{m.deleteFolderTitle(path)}</h3>
      <form noValidate onSubmit={remove}>
        <p>{m.deleteFolderBody}</p>
        <p>{m.deleteFolderServers(inside.length)}</p>
        {inside.length > 0 && (
          <ul aria-label={m.deleteFolderServersList}>
            {inside.map((s) => (
              <li key={s.path}>{s.path}</li>
            ))}
          </ul>
        )}
        <div className="actions">
          <button type="submit" className="danger" disabled={busy}>
            {busy ? m.deleting : m.deleteFolderConfirm}
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

// The actions on a folder, at the top of its panel: New folder, and Delete
// folder for a folder other than the home folder (path null) once its servers
// are known.
function FolderActions({
  path,
  servers,
  changes,
}: {
  path: string | null;
  servers: Server[] | null;
  changes: FolderChanges;
}) {
  const [opened, setOpened] = useState<"new" | "delete" | null>(null);
  const parent = path ?? "";
  return (
    <>
      <div role="group" aria-label={m.folderActions} className="actions">
        <button type="button" className="secondary" onClick={() => setOpened("new")}>
          {m.newFolder}
        </button>
        {/* Offered for any folder in the address. One typed by hand through a
            symbolic link (?folder=link/x) is refused by the server with
            path_through_link, and one spelt otherwise than its names on disk
            with path_name_differs: the tree never links there, so that is
            accepted. */}
        {path !== null && servers !== null && (
          <button type="button" className="secondary-danger" onClick={() => setOpened("delete")}>
            {m.deleteFolder}
          </button>
        )}
      </div>
      {opened === "new" && (
        <NewFolderForm
          parent={parent}
          onCreated={(folder) => changes.created(parent, folder)}
          onCancel={() => setOpened(null)}
        />
      )}
      {opened === "delete" && path !== null && servers !== null && (
        <DeleteFolderDialog
          path={path}
          servers={servers}
          onDeleted={() => changes.deleted(path)}
          onFailed={() => changes.deleteFailed(path)}
          onCancel={() => setOpened(null)}
        />
      )}
    </>
  );
}

// The selected folder: where it is, what can be done to it, and its Claude
// server or the offer to start one. Each of servers and startReason is null or
// undefined until it is known, and startReason null when a server can start.
export function FolderPanel({
  path,
  servers,
  startReason,
  onChange,
  changes,
}: {
  path: string;
  servers: Server[] | null;
  startReason: StartBlocked | null | undefined;
  onChange: () => void;
  changes: FolderChanges;
}) {
  const server = servers === null ? undefined : (servers.find((s) => s.path === path) ?? null);
  const known = server !== undefined && startReason !== undefined;
  return (
    <section aria-label={path} className="panel">
      <Breadcrumb path={path} />
      <h2>{lineage(path).at(-1)?.name ?? path}</h2>
      <FolderActions path={path} servers={servers} changes={changes} />
      <PanelSection title={m.serverTitle}>
        {!known && <p>{m.loading}</p>}
        {known && server && <ServerCard server={server} startReason={startReason} onChange={onChange} />}
        {known && !server && <p>{m.noServer}</p>}
        {known && !server && startReason === null && <StartServer path={path} onStarted={onChange} />}
        {known && !server && startReason !== null && <p>{startReason.start}</p>}
      </PanelSection>
    </section>
  );
}

// The panel while no folder is selected: the home folder, where a folder can be made.
export function HomePanel({ changes }: { changes: FolderChanges }) {
  return (
    <section aria-label={m.home} className="panel">
      <h2>{m.home}</h2>
      <FolderActions path={null} servers={null} changes={changes} />
      <p>{m.panelEmpty}</p>
    </section>
  );
}

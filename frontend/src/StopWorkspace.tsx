import { type FormEvent, type RefObject, useEffect, useRef, useState } from "react";

import { type ApiError, api } from "./api";
import { Dialog } from "./Dialog";
import { m } from "./messages";
import { asApiError, ProblemView } from "./ProblemView";

// Stop workspace: a plain button, offered where the server says the workspace may be stopped now
// (`stoppable`), and shown waiting, Stopping… and disabled, while a Stop of it is under way
// (`stopping`). It asks first. Its owner's Container card and an administrator's row on
// Everyone's workspaces show it, each given the answer as it shows a workspace (T). Once the
// workspace is stopped the button goes, so focus goes to `focusAfter`.
export function StopWorkspace<T>({
  workspace,
  stoppable,
  stopping,
  focusAfter,
  onStopped,
  onFailed,
}: {
  workspace: { id: number; name: string };
  stoppable: boolean;
  stopping: boolean;
  focusAfter: RefObject<HTMLElement | null>;
  onStopped: (workspace: T) => void;
  onFailed: () => void;
}) {
  const [asking, setAsking] = useState(false);
  // Set once the workspace is stopped: focus moves once the dialog has given it back.
  const refocus = useRef(false);
  useEffect(() => {
    if (asking || !refocus.current) return;
    refocus.current = false;
    focusAfter.current?.focus();
  });
  if (!stoppable && !stopping && !asking) return null;
  return (
    <>
      <button type="button" disabled={stopping} onClick={() => setAsking(true)}>
        {stopping ? m.stopWorkspaceBusy : m.stopWorkspace}
      </button>
      {asking && (
        <StopWorkspaceDialog<T>
          workspace={workspace}
          onStopped={(stopped) => {
            refocus.current = true;
            setAsking(false);
            onStopped(stopped);
          }}
          onFailed={onFailed}
          onCancel={() => setAsking(false)}
        />
      )}
    </>
  );
}

// Asks before a Stop: what it frees, what it ends and what it keeps. Its button says Stopping…
// and waits while the request runs, which lasts until the container is stopped; a refusal, or a
// container that did not stop, is shown in it.
function StopWorkspaceDialog<T>({
  workspace,
  onStopped,
  onFailed,
  onCancel,
}: {
  workspace: { id: number; name: string };
  onStopped: (workspace: T) => void;
  onFailed: () => void;
  onCancel: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  async function stop(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const answer = await api.post<{ workspace: T }>(`/api/workspaces/${workspace.id}/stop`);
      onStopped(answer.workspace);
    } catch (e) {
      setError(asApiError(e));
      setBusy(false);
      onFailed();
    }
  }

  return (
    <Dialog
      owner="StopWorkspaceDialog"
      title={m.stopWorkspaceTitle(workspace.name)}
      onClose={onCancel}
      closable={!busy}
    >
      <form noValidate onSubmit={stop}>
        <p>{m.stopWorkspaceIntro}</p>
        <p>{m.rebuildEndsTitle}</p>
        <ul className="rebuild-list">
          {m.stopWorkspaceEnds.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
        <p>{m.rebuildKeepsTitle}</p>
        <ul className="rebuild-list">
          {m.stopWorkspaceKeeps.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
        <ProblemView error={error} />
        <div className="actions footer">
          <button type="submit" className="primary" disabled={busy}>
            {busy ? m.stopWorkspaceBusy : m.stopWorkspaceConfirm}
          </button>
          <button type="button" disabled={busy} onClick={onCancel}>
            {m.cancel}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

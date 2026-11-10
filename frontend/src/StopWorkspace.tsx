import { type RefObject, useEffect, useRef, useState } from "react";

import { type ApiError, api } from "./api";
import { m } from "./messages";
import { asApiError, ProblemView } from "./ProblemView";

// Stop workspace: a plain button, offered where the server says the workspace may be stopped now
// (`stoppable`), and shown waiting, Stopping… and disabled, while a Stop of it is under way
// (`stopping`, or this request). It stops at once, without asking; a refusal, or a container that
// did not stop, is shown beside it. Its owner's Container card and an administrator's row on
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
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  // Set once the workspace is stopped: focus moves once the button has gone.
  const refocus = useRef(false);
  useEffect(() => {
    if (busy || !refocus.current) return;
    refocus.current = false;
    focusAfter.current?.focus();
  });

  async function stop() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const answer = await api.post<{ workspace: T }>(`/api/workspaces/${workspace.id}/stop`);
      refocus.current = true;
      setBusy(false);
      onStopped(answer.workspace);
    } catch (e) {
      setError(asApiError(e));
      setBusy(false);
      onFailed();
    }
  }

  if (!stoppable && !stopping && !busy && error === null) return null;
  return (
    <>
      {(stoppable || stopping || busy) && (
        <button type="button" disabled={stopping || busy} onClick={stop}>
          {stopping || busy ? m.stopWorkspaceBusy : m.stopWorkspace}
        </button>
      )}
      <ProblemView error={error} />
    </>
  );
}

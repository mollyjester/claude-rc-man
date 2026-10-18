import { type FormEvent, type ReactNode, useEffect, useId, useRef, useState } from "react";

import type { ApiError } from "./api";
import { Caret } from "./Caret";
import type { Branch, Branches } from "./Changes";
import { BranchIcon, CheckIcon, DownIcon, PlusIcon, RefreshIcon, UpIcon } from "./Icons";
import { m } from "./messages";
import { ProblemView, useFieldErrors } from "./ProblemView";
import { usePopup } from "./usePopup";

type Where = { name: string; shown: string };

// Where the repository is, as one sentence, and as the few words the bar
// shows: from the status, or, until it is known, from the current local
// branch; with neither, the lookup is named for the branches it lists.
function whereAt(branch: Branch | null, branches: Branches | null): Where {
  if (branch === null) {
    const current = branches?.local.branches.find((b) => b.current);
    return current
      ? { name: m.onBranch(current.name), shown: current.name }
      : { name: m.branchesTitle, shown: m.branchesTitle };
  }
  const commit = (branch.commit ?? "").slice(0, 7);
  if (branch.name === null) return { name: m.detachedAt(commit), shown: commit };
  if (branch.commit === null) return { name: m.noCommitsYet(branch.name), shown: branch.name };
  return { name: m.onBranch(branch.name), shown: branch.name };
}

// The branches listed, those whose name holds the filter, whatever the case.
const matching = <T extends { name: string }>(branches: T[], filter: string) =>
  branches.filter((b) => b.name.toLowerCase().includes(filter.toLowerCase()));

// One group of the lookup: its name, its rows, and how many more there are.
function Group({
  title,
  count,
  more,
  filter,
  children,
}: {
  title: string;
  count: number;
  more: number;
  filter: string;
  children: ReactNode;
}) {
  const id = useId();
  return (
    <>
      <p className="group" id={id}>
        {title}
      </p>
      <ul aria-labelledby={id}>
        {count === 0 ? (
          <li className="empty">{filter === "" ? m.noBranches : m.noBranchMatches}</li>
        ) : (
          children
        )}
      </ul>
      {more > 0 && <p className="more">{m.andMore(more)}</p>}
    </>
  );
}

// What the lookup holds while it is open: a filter over the branches listed,
// which asks rcman nothing, and the branches, each a switch where one is offered.
function LookupPanel({
  branches,
  error,
  busy,
  onChoose,
}: {
  branches: Branches | null;
  error: ApiError | null;
  busy: boolean;
  onChoose: (name: string, remote: boolean) => void;
}) {
  const [filter, setFilter] = useState("");
  const field = useRef<HTMLInputElement>(null);
  useEffect(() => field.current?.focus(), []);
  const local = matching(branches?.local.branches ?? [], filter);
  const remote = matching(branches?.remote.branches ?? [], filter);
  return (
    <>
      <input
        ref={field}
        type="text"
        aria-label={m.findBranch}
        placeholder={m.findBranch}
        autoComplete="off"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
      />
      <ProblemView error={error} />
      {branches === null && error === null && <p className="empty">{m.loading}</p>}
      {branches !== null && (
        <>
          <Group
            title={m.localBranches}
            count={local.length}
            more={branches.local.more}
            filter={filter}
          >
            {local.map((b) => (
              <li key={b.name}>
                {b.current ? (
                  <div className="row">
                    <CheckIcon />
                    <span className="row-text">
                      <span className="branch-name">{b.name}</span>
                      <span className="note">{m.currentBranch}</span>
                      {b.upstream && <span className="note">{m.tracks(b.upstream)}</span>}
                    </span>
                  </div>
                ) : (
                  <button
                    type="button"
                    className="row"
                    disabled={busy}
                    aria-label={m.switchTo(b.name)}
                    onClick={() => onChoose(b.name, false)}
                  >
                    <span className="no-icon" />
                    <span className="row-text">
                      <span className="branch-name">{b.name}</span>
                      {b.upstream && <span className="note">{m.tracks(b.upstream)}</span>}
                    </span>
                  </button>
                )}
              </li>
            ))}
          </Group>
          <Group
            title={m.remoteBranches}
            count={remote.length}
            more={branches.remote.more}
            filter={filter}
          >
            {remote.map((b) => (
              <li key={b.name}>
                {b.local !== null || b.taken_by !== null ? (
                  <div className="row">
                    <span className="no-icon" />
                    <span className="row-text">
                      <span className="branch-name">{b.name}</span>
                      <span className="note">
                        {b.local !== null ? m.trackedBy(b.local) : m.nameTakenBy(b.taken_by ?? "")}
                      </span>
                    </span>
                  </div>
                ) : (
                  <button
                    type="button"
                    className="row"
                    disabled={busy}
                    aria-label={m.switchTo(b.name)}
                    onClick={() => onChoose(b.name, true)}
                  >
                    <span className="no-icon" />
                    <span className="row-text">
                      <span className="branch-name">{b.name}</span>
                    </span>
                  </button>
                )}
              </li>
            ))}
          </Group>
        </>
      )}
    </>
  );
}

// The branch lookup: a button naming where the repository is, which opens the
// branches to switch to.
function Lookup({
  where,
  branches,
  error,
  busy,
  onSwitch,
}: {
  where: Where;
  branches: Branches | null;
  error: ApiError | null;
  busy: boolean;
  onSwitch: (name: string, remote: boolean) => void;
}) {
  const popup = usePopup("Lookup");
  return (
    <div className="popup-anchor" ref={popup.rootRef}>
      <button
        type="button"
        className="secondary branch-button"
        aria-label={where.name}
        title={where.name}
        {...popup.buttonProps}
      >
        <BranchIcon />
        <span className="branch-label">{where.shown}</span>
        <Caret />
      </button>
      {popup.open && (
        <div className="popover" {...popup.panelProps}>
          <LookupPanel
            branches={branches}
            error={error}
            busy={busy}
            onChoose={(name, remote) => {
              popup.close(true);
              onSwitch(name, remote);
            }}
          />
        </div>
      )}
    </div>
  );
}

// New branch: a + that opens the form for its name. Opening or closing it
// takes any refusal away, one that arrived while it was closed too. An answer
// that arrives once it was left closes nothing and moves no focus; a refusal
// puts focus back in the field, since the button that had it waited.
function NewBranch({
  busy,
  error,
  onCreate,
  onForget,
}: {
  busy: boolean;
  error: ApiError | null;
  onCreate: (name: string) => Promise<boolean>;
  onForget: () => void;
}) {
  const popup = usePopup("NewBranch");
  const [name, setName] = useState("");
  const field = useRef<HTMLInputElement>(null);
  const fields = useFieldErrors(error);

  useEffect(() => {
    onForget();
    if (popup.open) field.current?.focus();
  }, [popup.open, onForget]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const made = await onCreate(name);
    if (made) setName("");
    if (!popup.isOpen()) return;
    if (made) popup.close(true);
    else field.current?.focus();
  }

  return (
    <div className="popup-anchor" ref={popup.rootRef}>
      <button
        type="button"
        className="secondary icon-button"
        aria-label={m.newBranch}
        title={m.newBranch}
        disabled={busy}
        {...popup.buttonProps}
      >
        <PlusIcon />
      </button>
      {popup.open && (
        <div className="popover" {...popup.panelProps}>
          <form noValidate onSubmit={submit} ref={fields.form}>
            <label>
              {m.newBranchName}
              <input
                ref={field}
                type="text"
                name="name"
                autoComplete="off"
                value={name}
                onChange={(e) => setName(e.target.value)}
                {...fields.input("name")}
              />
            </label>
            {fields.message("name")}
            <div className="actions">
              <button type="submit" className="primary" disabled={busy}>
                {m.create}
              </button>
              <button type="button" onClick={() => popup.close(true)}>
                {m.cancel}
              </button>
            </div>
            <ProblemView error={error} labels={{ name: m.newBranchName }} inline />
          </form>
        </div>
      )}
    </div>
  );
}

// How far the branch is from its upstream, as arrows for the eye and as
// today's sentence for everything else; where the upstream is gone, the
// sentence alone. Nothing for a branch with no upstream: rcman knows only
// whether one is set, not whether the branch is on the remote.
function SyncState({ branch }: { branch: Branch }) {
  if (branch.upstream === null) return null;
  const sentence = m.tracking(branch.upstream, branch.ahead, branch.behind);
  if (branch.ahead === null || branch.behind === null)
    return <span className="sync">{sentence}</span>;
  return (
    <span className="sync" title={sentence}>
      <span className="sync-arrows" aria-hidden="true">
        <span className={branch.ahead > 0 ? "some" : undefined}>
          <UpIcon />
          {branch.ahead}
        </span>
        <span className={branch.behind > 0 ? "some" : undefined}>
          <DownIcon />
          {branch.behind}
        </span>
      </span>
      <span className="visually-hidden">{sentence}</span>
    </span>
  );
}

// The bar beside the Changes heading: the branch lookup, New branch and Refresh
// branches, which need only the branches; and, once the status is known, how
// far the branch is from its upstream and Push where it is offered. Where a
// sync can run (syncable), Refresh branches is Sync instead: each label calls
// its own route, so the button never does more than it says.
export function BranchBar({
  branch,
  branches,
  branchesError,
  busy,
  fetching,
  syncable,
  syncing,
  pushOffered,
  createError,
  onSwitch,
  onCreate,
  onCreateForget,
  onFetch,
  onSync,
  onPush,
}: {
  branch: Branch | null;
  branches: Branches | null;
  branchesError: ApiError | null;
  busy: boolean;
  fetching: boolean;
  syncable: boolean;
  syncing: boolean;
  pushOffered: boolean;
  createError: ApiError | null;
  onSwitch: (name: string, remote: boolean) => void;
  onCreate: (name: string) => Promise<boolean>;
  onCreateForget: () => void;
  onFetch: () => void;
  onSync: () => void;
  onPush: () => void;
}) {
  const refreshLabel = syncable ? m.sync : m.refreshBranches;
  return (
    <div className="branch-bar">
      <Lookup
        where={whereAt(branch, branches)}
        branches={branches}
        error={branchesError}
        busy={busy}
        onSwitch={onSwitch}
      />
      <NewBranch busy={busy} error={createError} onCreate={onCreate} onForget={onCreateForget} />
      <button
        type="button"
        className={fetching || syncing ? "secondary icon-button spinning" : "secondary icon-button"}
        aria-label={refreshLabel}
        title={refreshLabel}
        disabled={busy}
        onClick={syncable ? onSync : onFetch}
      >
        <RefreshIcon />
      </button>
      {branch !== null && <SyncState branch={branch} />}
      {pushOffered && (
        <button type="button" className="secondary" disabled={busy} onClick={onPush}>
          {m.push}
        </button>
      )}
    </div>
  );
}

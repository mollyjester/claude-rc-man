import {
  type DragEvent,
  type FormEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { Link, useSearchParams } from "react-router-dom";

import { ApiError, api, downloadUrl } from "./api";
import { browser } from "./browser";
import { Dialog } from "./Dialog";
import { formatAge, formatBytes, formatCount, utcMoment } from "./format";
import { DownloadIcon, FileIcon, FolderIcon, LinkIcon, RefreshIcon, UploadIcon } from "./Icons";
import { MessageBar } from "./MessageBar";
import { m } from "./messages";
import { asApiError, ProblemView } from "./ProblemView";

// A folder's entry, as GET /api/workspaces/{id}/files gives it (backend/rcman/routers/files.py):
// its size a file's only, when it last changed by the workspace's clock, and where a link points
// (null where that cannot be reached from here).
type Entry = {
  name: string;
  kind: "folder" | "file" | "link" | "other";
  size: number | null;
  changed: number;
  target: "folder" | "file" | "other" | null;
};
type Listing = {
  path: string;
  entries: Entry[];
  more: number;
  now: number;
  reserved?: string[];
};
// A refusal of a row's download, shown under it.
type Refused = { name: string; error: ApiError };
// The question before an upload: the files to send, and the names among them already there.
type Asking = { files: File[]; existing: string[]; folder: string };

const ICONS = { folder: FolderIcon, file: FileIcon, link: LinkIcon, other: FileIcon };
// What the server answers an upload over a file's name sent without overwrite.
const FILE_EXISTS = "file_exists";
// A megabyte as the server counts the limit: a million bytes.
const MB = 1_000_000;

const join = (folder: string, name: string) => (folder === "" ? name : `${folder}/${name}`);
const opensAsFolder = (e: Entry) =>
  e.kind === "folder" || (e.kind === "link" && e.target === "folder");
const downloadable = (e: Entry) =>
  opensAsFolder(e) || e.kind === "file" || (e.kind === "link" && e.target === "file");

// The Files tab of a ready workspace: one card of the folder the address names (`?path=`, the
// top where it names none), its crumbs leading back up, Upload and Refresh at their side, and its
// entries, folders first as the server lists them, each with its size, the time since it changed
// and Download; the row the address marks (`&file=`) is marked, and focus starts on it. A
// download is looked at first, and a refusal is shown under its row; Upload, or files dropped on
// the card, sends files into the folder shown one by one, after one question for the names
// already there. While the workspace is not up (`power`, the code the listing gives for its
// server) it says so, and asks for nothing. While it is `shown`, a file dropped anywhere on the
// page is not opened by the browser.
export function Files({
  workspace,
  shown,
  power,
}: {
  workspace: number;
  shown: boolean;
  power: string | null;
}) {
  const [params] = useSearchParams();
  const path = params.get("path") ?? "";
  const marked = params.get("file");
  const [listing, setListing] = useState<Listing | null>(null);
  const [refusal, setRefusal] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(false);
  const [preparing, setPreparing] = useState<string | null>(null);
  const [refused, setRefused] = useState<Refused | null>(null);
  const [asking, setAsking] = useState<Asking | null>(null);
  const [uploading, setUploading] = useState(false);
  const [status, setStatus] = useState("");
  const [failures, setFailures] = useState<Refused[]>([]);
  const [kept, setKept] = useState<string[]>([]);
  const [foldersSkipped, setFoldersSkipped] = useState(false);
  const [dropping, setDropping] = useState(false);
  const list = useRef<HTMLUListElement>(null);
  const chooser = useRef<HTMLInputElement>(null);
  const upload = useRef<HTMLButtonElement>(null);
  // Set as a folder is opened from the card, which moves focus to the list.
  const opened = useRef(false);
  // The folder and file the marked row was last brought into view for.
  const landed = useRef<string | null>(null);
  // The folder shown now, and its listing, for what ends after another may have been opened.
  const shownPath = useRef(path);
  shownPath.current = path;

  // An answer older than one asked for since is dropped.
  const latest = useRef(0);
  const load = useCallback(async () => {
    const mine = ++latest.current;
    setLoading(true);
    try {
      const answer = await api.files.list<Listing>(workspace, path);
      if (mine !== latest.current) return;
      setListing(answer);
      setRefusal(null);
    } catch (e) {
      if (mine !== latest.current) return;
      setListing(null);
      setRefusal(asApiError(e));
    } finally {
      if (mine === latest.current) setLoading(false);
    }
  }, [workspace, path]);

  const loadNow = useRef(load);
  loadNow.current = load;

  useEffect(() => {
    if (!power) void load();
  }, [load, power]);

  // What was said of the folder before is not said of another.
  // biome-ignore lint/correctness/useExhaustiveDependencies: cleared whenever the folder shown changes
  useEffect(() => {
    setRefused(null);
    setStatus("");
    setFailures([]);
    setKept([]);
    setFoldersSkipped(false);
    if (opened.current) {
      opened.current = false;
      list.current?.focus();
    }
  }, [path]);

  // Landing on a marked row: it is brought into view and its Download takes focus, once.
  useEffect(() => {
    if (!shown || marked === null || listing?.path !== path) return;
    const at = `${path}\n${marked}`;
    if (landed.current === at) return;
    landed.current = at;
    const row = list.current?.querySelector<HTMLElement>('li[aria-current="true"]');
    row?.scrollIntoView?.({ block: "nearest" });
    (row?.querySelector<HTMLElement>("button") ?? row?.querySelector("a") ?? list.current)?.focus();
  }, [shown, marked, path, listing]);

  // While the tab is shown, a file dropped outside the card is not opened by the browser.
  useEffect(() => {
    if (!shown) return;
    const keep = (event: globalThis.DragEvent) => {
      if (event.dataTransfer?.types.includes("Files")) event.preventDefault();
    };
    window.addEventListener("dragover", keep);
    window.addEventListener("drop", keep);
    return () => {
      window.removeEventListener("dragover", keep);
      window.removeEventListener("drop", keep);
    };
  }, [shown]);

  // The address of a folder of the card: the folder in place of the one shown, no row marked.
  const at = (folder: string) => {
    const next = new URLSearchParams(params);
    if (folder === "") next.delete("path");
    else next.set("path", folder);
    next.delete("file");
    return { search: `?${next}` };
  };
  const open = () => {
    opened.current = true;
  };

  async function download(entry: Entry) {
    if (preparing !== null || listing === null) return;
    const full = join(listing.path, entry.name);
    setRefused(null);
    setPreparing(entry.name);
    try {
      await api.files.check(workspace, full);
      browser.download(downloadUrl(workspace, full));
    } catch (e) {
      // said only beside a row of the folder still shown
      if (shownPath.current === listing.path)
        setRefused({ name: entry.name, error: asApiError(e) });
    } finally {
      setPreparing(null);
    }
  }

  // Files chosen or dropped into the folder shown: the names rcman keeps there are refused, a
  // dropped folder is skipped, and the names already there are asked about once.
  function chosen(files: File[], folders: number) {
    if (listing === null || uploading) return;
    const reserved = listing.reserved ?? [];
    const going = files.filter((f) => !reserved.includes(f.name));
    setKept(files.filter((f) => reserved.includes(f.name)).map((f) => f.name));
    setFoldersSkipped(folders > 0);
    setFailures([]);
    setStatus("");
    const there = new Set(listing.entries.filter((e) => e.kind === "file").map((e) => e.name));
    const existing = going.filter((f) => there.has(f.name)).map((f) => f.name);
    if (existing.length > 0) {
      // The dialog gives focus back to Upload, whatever had it: after a drop, nothing did.
      upload.current?.focus();
      setAsking({ files: going, existing, folder: listing.path });
    } else void send(going, [], listing.path);
  }

  // Each file in turn, into `folder`, those named in `replacing` with overwrite: a failure is
  // named and the rest go on; a name the listing did not show that is already there is asked
  // about once the others have gone; then the folder is listed again. Once another folder is
  // shown, nothing more is said of this one, nor is it listed.
  async function send(files: File[], replacing: string[], folder: string) {
    if (files.length === 0) return;
    const here = () => shownPath.current === folder;
    setUploading(true);
    const there: File[] = [];
    let done = 0;
    for (const [i, file] of files.entries()) {
      if (here()) setStatus(m.filesUploading(file.name, i + 1, files.length));
      const replace = replacing.includes(file.name);
      try {
        await api.files.upload(workspace, folder, file, replace);
        done += 1;
      } catch (e) {
        const error = asApiError(e);
        if (error.code === FILE_EXISTS && !replace) there.push(file);
        else {
          // beside those told before, as of the files held back to be asked about first; a new
          // batch (chosen) clears them
          if (here()) setFailures((told) => [...told, { name: file.name, error }]);
        }
      }
    }
    setUploading(false);
    if (!here()) return;
    const sent = files.length - there.length;
    if (sent > 1) setStatus(m.filesUploadedCount(done, sent));
    else
      setStatus(
        done === 1 ? m.filesUploaded(files.find((f) => !there.includes(f))?.name ?? "") : "",
      );
    void loadNow.current();
    if (there.length > 0) {
      upload.current?.focus();
      setAsking({ files: there, existing: there.map((f) => f.name), folder });
    }
  }

  function answered(replace: boolean) {
    if (asking === null) return;
    const { files, existing, folder } = asking;
    setAsking(null);
    void send(
      replace ? files : files.filter((f) => !existing.includes(f.name)),
      replace ? existing : [],
      folder,
    );
  }

  const up = !power;
  const current = listing !== null && listing.path === path;
  const canUpload = up && current && !uploading;

  function dragging(event: DragEvent) {
    if (!canUpload || !event.dataTransfer?.types.includes("Files")) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    setDropping(true);
  }
  function left(event: DragEvent) {
    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDropping(false);
  }
  function dropped(event: DragEvent) {
    event.preventDefault();
    setDropping(false);
    if (!canUpload) return;
    const { files, folders } = droppedFiles(event.dataTransfer);
    chosen(files, folders);
  }

  let body: ReactNode;
  if (power)
    body = (
      <p className="card-empty muted">{m.filesPower[power] ?? m.filesPower.workspace_stopped}</p>
    );
  else if (refusal)
    body = (
      <div className="file-browser-refusal">
        <ProblemView error={refusal} />
        <p className="muted">{m.filesGoUp}</p>
      </div>
    );
  else if (listing === null)
    body = (
      <p className="card-empty muted" aria-busy="true">
        {m.filesLoading}
      </p>
    );
  else
    body = (
      <>
        <ul
          ref={list}
          className="entries"
          aria-label={m.filesAt(listing.path)}
          aria-busy={loading && !current}
          tabIndex={-1}
        >
          {listing.entries.map((entry) => (
            <EntryRow
              key={entry.name}
              entry={entry}
              now={listing.now}
              marked={current && entry.name === marked}
              preparing={preparing === entry.name}
              refused={refused?.name === entry.name ? refused.error : null}
              to={at(join(listing.path, entry.name))}
              onOpen={open}
              onDownload={() => download(entry)}
            />
          ))}
        </ul>
        {listing.more > 0 && (
          <p className="card-empty muted">{m.filesMore(formatCount(listing.more))}</p>
        )}
        {listing.entries.length === 0 && listing.more === 0 && (
          <div className="empty-state">
            <span className="empty-icon">
              <FolderIcon />
            </span>
            <strong>{m.filesEmpty}</strong>
            <p>{m.filesEmptyBody}</p>
            <button
              type="button"
              className="primary"
              aria-disabled={!canUpload}
              onClick={() => canUpload && chooser.current?.click()}
            >
              <UploadIcon />
              {m.filesUpload}
            </button>
          </div>
        )}
      </>
    );

  const parts = path === "" ? [] : path.split("/");
  return (
    // biome-ignore lint/a11y/noStaticElementInteractions: the card takes files dropped on it; Upload does the same for keyboard and touch
    <div
      className={dropping ? "card flush file-browser dropping" : "card flush file-browser"}
      onDragEnter={dragging}
      onDragOver={dragging}
      onDragLeave={left}
      onDrop={dropped}
    >
      <div className="card-head file-browser-head">
        <nav aria-label={m.filesPath} className="crumbs">
          <ol>
            <li>
              <Link
                to={at("")}
                replace
                onClick={open}
                translate="no"
                aria-current={parts.length === 0 ? "location" : undefined}
              >
                {m.filesTop}
              </Link>
            </li>
            {parts.map((part, i) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: a part's place is what it is; two parts can have one name
              <li key={i}>
                <span className="crumb-divider" aria-hidden="true">
                  /
                </span>
                <Link
                  to={at(parts.slice(0, i + 1).join("/"))}
                  replace
                  onClick={open}
                  translate="no"
                  aria-current={i === parts.length - 1 ? "location" : undefined}
                >
                  {part}
                </Link>
              </li>
            ))}
          </ol>
        </nav>
        {up && (
          <div className="file-browser-actions">
            {/* kept while another folder is asked for, waiting until it is listed */}
            {listing !== null && (
              <button
                ref={upload}
                type="button"
                className="subtle"
                aria-disabled={!canUpload}
                onClick={() => canUpload && chooser.current?.click()}
              >
                <UploadIcon />
                {m.filesUpload}
              </button>
            )}
            <button
              type="button"
              className={loading ? "subtle icon-button spinning" : "subtle icon-button"}
              aria-label={m.filesRefresh}
              title={m.filesRefresh}
              onClick={() => void load()}
            >
              <RefreshIcon />
            </button>
          </div>
        )}
      </div>
      <p className="card-status" role="status">
        {status}
      </p>
      {failures.map(({ name, error }) => (
        <ProblemView
          key={name}
          error={
            new ApiError(
              error.status,
              error.code,
              `${m.filesUploadFailed(name)} ${error.title}`,
              error.errors,
              error.extra,
            )
          }
        />
      ))}
      {kept.length > 0 && (
        <MessageBar kind="problem" role="alert">
          {kept.map((name) => (
            <p key={name}>{m.filesReserved(name)}</p>
          ))}
        </MessageBar>
      )}
      {foldersSkipped && (
        <MessageBar kind="warning" role="alert">
          <p>{m.filesNoFolders}</p>
        </MessageBar>
      )}
      <div className="file-browser-body">
        {body}
        {dropping && (
          <p className="drop-line" aria-hidden="true">
            <UploadIcon />
            {m.filesDrop(path)}
          </p>
        )}
      </div>
      <input
        ref={chooser}
        type="file"
        multiple
        hidden
        tabIndex={-1}
        aria-hidden="true"
        onChange={(event) => {
          const files = [...(event.target.files ?? [])];
          event.target.value = "";
          chosen(files, 0);
        }}
      />
      {asking && (
        <ReplaceDialog
          names={asking.existing}
          folder={asking.folder}
          onAnswer={answered}
          onCancel={() => setAsking(null)}
        />
      )}
    </div>
  );
}

// What was dropped: its files, and how many folders, which are not uploaded. A browser that
// cannot tell a folder from a file gives each as a file, which the server then refuses.
function droppedFiles(data: DataTransfer): { files: File[]; folders: number } {
  const items = data.items ? [...data.items].filter((item) => item.kind === "file") : [];
  if (items.length === 0) return { files: [...(data.files ?? [])], folders: 0 };
  const files: File[] = [];
  let folders = 0;
  for (const item of items) {
    if (item.webkitGetAsEntry?.()?.isDirectory) folders += 1;
    else {
      const file = item.getAsFile();
      if (file) files.push(file);
    }
  }
  return { files, folders };
}

// An entry's row: its kind's icon, its name, a folder's a link that opens it, its size and the
// time since it changed, and Download, named after it; a link pointing out of reach says so in
// their place. While its download is looked at, Download waits, keeping focus, and a refusal is
// shown under the row.
function EntryRow({
  entry,
  now,
  marked,
  preparing,
  refused,
  to,
  onOpen,
  onDownload,
}: {
  entry: Entry;
  now: number;
  marked: boolean;
  preparing: boolean;
  refused: ApiError | null;
  to: { search: string };
  onOpen: () => void;
  onDownload: () => void;
}) {
  const Icon = ICONS[entry.kind];
  const folder = opensAsFolder(entry);
  const outside = entry.kind === "link" && entry.target === null;
  const changed = new Date(entry.changed * 1000).toISOString();
  return (
    <li
      className={marked ? "list-row entry selected" : "list-row entry"}
      aria-current={marked ? "true" : undefined}
    >
      <span className="entry-icon" role="img" aria-label={m.filesKinds[entry.kind]}>
        <Icon />
      </span>
      <span className="entry-main">
        {folder ? (
          <Link className="entry-name" to={to} replace onClick={onOpen} translate="no">
            {entry.name}
          </Link>
        ) : (
          <span className="entry-name" translate="no">
            {entry.name}
          </span>
        )}
        {outside ? (
          <span className="entry-meta muted">{m.filesLinkOutside}</span>
        ) : (
          <span className="entry-meta muted">
            <span className="entry-size">{entry.size === null ? "" : formatBytes(entry.size)}</span>
            <span className="entry-age" title={`${utcMoment(changed)} UTC`}>
              {formatAge(now - entry.changed)}
            </span>
          </span>
        )}
      </span>
      <span className="entry-action">
        {preparing && <span className="muted">{m.filesPreparing}</span>}
        {downloadable(entry) && (
          <button
            type="button"
            className={preparing ? "subtle icon-button spinning" : "subtle icon-button"}
            aria-label={folder ? m.filesDownloadFolder(entry.name) : m.filesDownload(entry.name)}
            title={folder ? m.filesDownloadFolder(entry.name) : m.filesDownload(entry.name)}
            aria-disabled={preparing || undefined}
            onClick={() => !preparing && onDownload()}
          >
            {preparing ? <RefreshIcon /> : <DownloadIcon />}
          </button>
        )}
      </span>
      {refused && <RowRefusal name={entry.name} error={refused} />}
    </li>
  );
}

// Why a row cannot be downloaded: a folder over the limit with its size and the limit, one with
// something in it that cannot be read with what and what to do, else as the server said.
function RowRefusal({ name, error }: { name: string; error: ApiError }) {
  const { size, limit_mb: limit, path } = error.extra;
  let said: ReactNode = null;
  if (error.code === "folder_too_big" && typeof size === "number" && typeof limit === "number")
    said = <p>{m.filesTooBig(name, formatBytes(size), formatBytes(limit * MB))}</p>;
  else if (error.code === "folder_unreadable" && typeof path === "string")
    said = (
      <>
        <p>{m.filesUnreadable(name, path)}</p>
        <p>{m.filesUnreadableNext}</p>
      </>
    );
  return (
    <div className="entry-problem">
      {said === null ? (
        <ProblemView error={error} />
      ) : (
        <MessageBar kind="problem" role="alert">
          {said}
        </MessageBar>
      )}
    </div>
  );
}

// Asks once before files replace those of the same names in the folder: Replace sends them all,
// Skip sends only the others, and closing it sends nothing.
function ReplaceDialog({
  names,
  folder,
  onAnswer,
  onCancel,
}: {
  names: string[];
  folder: string;
  onAnswer: (replace: boolean) => void;
  onCancel: () => void;
}) {
  const one = names.length === 1;
  function replace(event: FormEvent) {
    event.preventDefault();
    onAnswer(true);
  }
  return (
    <Dialog
      owner="ReplaceDialog"
      title={one ? m.filesReplaceTitle(names[0]) : m.filesReplaceTitleCount(names.length)}
      onClose={onCancel}
    >
      <form noValidate onSubmit={replace}>
        <p>{one ? m.filesReplaceBody(names[0], folder) : m.filesReplaceBodyCount(folder)}</p>
        {!one && (
          <ul className="replace-names" translate="no">
            {names.map((name) => (
              <li key={name}>{name}</li>
            ))}
          </ul>
        )}
        <div className="actions footer">
          <button type="submit" className="danger">
            {m.filesReplace}
          </button>
          <button type="button" onClick={() => onAnswer(false)}>
            {m.filesSkip}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

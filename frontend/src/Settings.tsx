import { type FormEvent, type ReactNode, useEffect, useId, useState } from "react";

import { ApiError, api } from "./api";
import { m } from "./messages";
import { ProblemView, asApiError } from "./ProblemView";

type Field = "prefix" | "author_name" | "author_email";
type Reply = Record<Field, string> & { effective_author: { name: string; email: string } | null };

// Every field the page shows; docs/settings.md lists each of them.
export const FIELDS: Record<Field, { label: string; help: string }> = {
  prefix: { label: m.prefixLabel, help: m.prefixHelp },
  author_name: { label: m.authorNameLabel, help: m.authorNameHelp },
  author_email: { label: m.authorEmailLabel, help: m.authorEmailHelp },
};

export function Settings() {
  const [reply, setReply] = useState<Reply | null>(null);
  const [error, setError] = useState<ApiError | null>(null);

  useEffect(() => {
    api.get<Reply>("/api/me/settings").then(setReply, (e) => setError(asApiError(e)));
  }, []);

  const author = reply?.effective_author;
  return (
    <section>
      <h1>{m.settingsTitle}</h1>
      <ProblemView error={error} />
      {reply && (
        <>
          <Part
            title={m.settingsPrefixTitle}
            fields={["prefix"]}
            reply={reply}
            onSaved={setReply}
          />
          <Part
            title={m.settingsAuthorTitle}
            fields={["author_name", "author_email"]}
            reply={reply}
            onSaved={setReply}
          >
            <p>{author ? m.authorEffective(author.name, author.email) : m.authorNotSet}</p>
          </Part>
        </>
      )}
      <GitHubToken />
    </section>
  );
}

// The GitHub token: whether one is stored, a field to store one, never filled
// from the server, and Remove, which asks first.
function GitHubToken() {
  const [stored, setStored] = useState<boolean | null>(null);
  const [token, setToken] = useState("");
  const [saved, setSaved] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const heading = useId();

  useEffect(() => {
    api.get<{ token: boolean }>("/api/github").then(
      (answer) => setStored(answer.token === true),
      (e) => setError(asApiError(e)),
    );
  }, []);

  async function save(event: FormEvent) {
    event.preventDefault();
    setSaved(false);
    setError(null);
    try {
      const answer = await api.post<{ token: boolean }>("/api/github/token", { token });
      setToken("");
      setStored(answer.token === true);
      setSaved(true);
    } catch (e) {
      setError(asApiError(e));
    }
  }

  return (
    <section aria-labelledby={heading}>
      <h2 id={heading}>{m.settingsGithubTitle}</h2>
      {stored !== null && <p>{stored ? m.githubTokenStored : m.githubTokenNotStored}</p>}
      <form noValidate onSubmit={save}>
        <div>
          <label>
            {m.githubTokenLabel}
            <input
              type="password"
              name="token"
              autoComplete="new-password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
            />
          </label>
          <p className="muted">{m.githubTokenHelp}</p>
        </div>
        <button type="submit" className="primary">
          {m.save}
        </button>
      </form>
      {stored && (
        <div className="actions">
          <button
            type="button"
            className="secondary-danger"
            onClick={() => {
              setSaved(false);
              setRemoving(true);
            }}
          >
            {m.githubTokenRemove}
          </button>
        </div>
      )}
      {removing && (
        <RemoveTokenDialog
          onRemoved={() => {
            setRemoving(false);
            setSaved(false);
            setStored(false);
          }}
          onCancel={() => setRemoving(false)}
        />
      )}
      {saved && <p role="status">{m.githubTokenSaved}</p>}
      <ProblemView error={error} labels={{ token: m.githubTokenLabel }} />
    </section>
  );
}

function RemoveTokenDialog({
  onRemoved,
  onCancel,
}: {
  onRemoved: () => void;
  onCancel: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  async function remove(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.delete("/api/github/token");
      onRemoved();
    } catch (e) {
      setError(asApiError(e));
      setBusy(false);
    }
  }

  return (
    <div role="dialog" aria-label={m.githubTokenRemoveTitle} className="dialog">
      <h3>{m.githubTokenRemoveTitle}</h3>
      <form noValidate onSubmit={remove}>
        <p>{m.githubTokenRemoveBody}</p>
        <div className="actions">
          <button type="submit" className="danger" disabled={busy}>
            {m.githubTokenRemoveConfirm}
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

// One part of the page: its own fields, saved on their own.
function Part(props: {
  title: string;
  fields: Field[];
  reply: Reply;
  onSaved: (reply: Reply) => void;
  children?: ReactNode;
}) {
  const { title, fields, reply, onSaved, children } = props;
  const [values, setValues] = useState(() => Object.fromEntries(fields.map((f) => [f, reply[f]])));
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const heading = useId();

  async function save(event: FormEvent) {
    event.preventDefault();
    setSaved(false);
    setError(null);
    try {
      const next = await api.patch<Reply>("/api/me/settings", values);
      setValues(Object.fromEntries(fields.map((f) => [f, next[f]])));
      onSaved(next);
      setSaved(true);
    } catch (e) {
      setError(asApiError(e));
    }
  }

  return (
    <section aria-labelledby={heading}>
      <h2 id={heading}>{title}</h2>
      <form noValidate onSubmit={save}>
        {fields.map((field) => (
          <div key={field}>
            <label>
              {FIELDS[field].label}
              <input
                type="text"
                name={field}
                value={values[field]}
                onChange={(e) => setValues({ ...values, [field]: e.target.value })}
              />
            </label>
            <p className="muted">{FIELDS[field].help}</p>
          </div>
        ))}
        <button type="submit" className="primary">
          {m.save}
        </button>
      </form>
      {children}
      {saved && <p role="status">{m.saved}</p>}
      <ProblemView
        error={error}
        labels={Object.fromEntries(fields.map((f) => [f, FIELDS[f].label]))}
      />
    </section>
  );
}

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
          <Part title={m.settingsPrefixTitle} fields={["prefix"]} reply={reply} onSaved={setReply} />
          <Part title={m.settingsAuthorTitle} fields={["author_name", "author_email"]} reply={reply} onSaved={setReply}>
            <p>{author ? m.authorEffective(author.name, author.email) : m.authorNotSet}</p>
          </Part>
        </>
      )}
    </section>
  );
}

// One part of the page: its own fields, saved on their own.
function Part(props: { title: string; fields: Field[]; reply: Reply; onSaved: (reply: Reply) => void; children?: ReactNode }) {
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
      <ProblemView error={error} />
    </section>
  );
}

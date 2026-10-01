import { type FormEvent, useEffect, useState } from "react";

import { ApiError, api } from "./api";
import { m } from "./messages";
import { ProblemView, asApiError } from "./ProblemView";

export function Settings() {
  const [prefix, setPrefix] = useState("");
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  useEffect(() => {
    api.get<{ prefix: string }>("/api/me/settings").then((s) => setPrefix(s.prefix), (e) => setError(asApiError(e)));
  }, []);

  async function save(event: FormEvent) {
    event.preventDefault();
    setSaved(false);
    setError(null);
    try {
      setPrefix((await api.put<{ prefix: string }>("/api/me/settings", { prefix })).prefix);
      setSaved(true);
    } catch (e) {
      setError(asApiError(e));
    }
  }

  return (
    <section>
      <h1>{m.settingsTitle}</h1>
      <form noValidate onSubmit={save}>
        <label>
          {m.prefixLabel}
          <input type="text" value={prefix} onChange={(e) => setPrefix(e.target.value)} />
        </label>
        <p className="muted">{m.prefixHelp}</p>
        <button type="submit">{m.save}</button>
      </form>
      {saved && <p role="status">{m.saved}</p>}
      <ProblemView error={error} />
    </section>
  );
}

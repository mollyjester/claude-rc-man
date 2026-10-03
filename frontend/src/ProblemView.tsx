import { ApiError } from "./api";
import { m } from "./messages";

// Shows an error exactly as the server described it. A form passes its labels,
// by field name, so a refused field is named as the form names it.
export function ProblemView({ error, labels = {} }: { error: ApiError | null; labels?: Record<string, string> }) {
  if (!error) return null;
  const screen = error.extra.screen;
  const failed = error.extra.failed;
  const left = error.extra.left;
  const git = error.extra.git;
  const gitMore = error.extra.git_more;
  // The server a problem is about, as rc-helper describes one, when it names one.
  const server = error.extra.server as { path?: unknown; screen?: unknown } | null | undefined;
  const serverPath = typeof server === "object" && server !== null && typeof server.path === "string" ? server.path : null;
  return (
    <div role="alert" className="problem">
      <p>{error.title}</p>
      {error.errors.length > 0 && (
        <ul aria-label={m.problemFieldErrors}>
          {error.errors.map((e, i) => (
            <li key={i}>{`${labels[e.field] ?? e.field}: ${e.message}`}</li>
          ))}
        </ul>
      )}
      {Array.isArray(failed) && (
        <p>
          {m.versionsFailedChecks} {failed.join(", ")}
        </p>
      )}
      {Array.isArray(screen) && <pre className="screen">{screen.join("\n")}</pre>}
      {serverPath !== null && (
        <>
          <p>
            {m.problemServer} <code>{serverPath}</code>
          </p>
          {Array.isArray(server?.screen) && <pre className="screen">{server.screen.join("\n")}</pre>}
        </>
      )}
      {Array.isArray(git) && (
        <>
          <pre className="screen git-said" aria-label={m.gitSaid}>
            {git.join("\n")}
          </pre>
          {typeof gitMore === "number" && gitMore > 0 && <p>{m.andMoreLines(gitMore)}</p>}
        </>
      )}
      {Array.isArray(left) && (
        <>
          <p>{m.problemLeft}</p>
          <ul aria-label={m.problemLeft}>
            {left.map((path, i) => (
              <li key={i}>{String(path)}</li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

export function asApiError(error: unknown): ApiError {
  return error instanceof ApiError ? error : new ApiError(0, "unexpected_response", m.unexpectedResponse);
}

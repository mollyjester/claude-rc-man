import { ApiError, UNEXPECTED_RESPONSE } from "./api";
import { noteFailure, PAGE_ERROR } from "./clientErrors";
import { m } from "./messages";

// Shows an error exactly as the server described it. A form passes its labels,
// by field name, so a refused field is named as the form names it.
export function ProblemView({
  error,
  labels = {},
}: {
  error: ApiError | null;
  labels?: Record<string, string>;
}) {
  if (!error) return null;
  const screen = error.extra.screen;
  const failed = error.extra.failed;
  const left = error.extra.left;
  const git = error.extra.git;
  const gitMore = error.extra.git_more;
  // The server a problem is about, as rc-helper describes one, when it names one.
  const server = error.extra.server as { path?: unknown; screen?: unknown } | null | undefined;
  const serverPath =
    typeof server === "object" && server !== null && typeof server.path === "string"
      ? server.path
      : null;
  return (
    <div role="alert" className="problem">
      <p>{error.title}</p>
      {error.errors.length > 0 && (
        <ul aria-label={m.problemFieldErrors}>
          {error.errors.map((e, i) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: the server's errors, shown whole and replaced whole; they can repeat, and never move
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
          {Array.isArray(server?.screen) && (
            <pre className="screen">{server.screen.join("\n")}</pre>
          )}
        </>
      )}
      {Array.isArray(git) && (
        <>
          <figure aria-label={m.gitSaid}>
            <pre className="screen git-said">{git.join("\n")}</pre>
          </figure>
          {typeof gitMore === "number" && gitMore > 0 && <p>{m.andMoreLines(gitMore)}</p>}
        </>
      )}
      {Array.isArray(left) && (
        <>
          <p>{m.problemLeft}</p>
          <ul aria-label={m.problemLeft}>
            {left.map((path, i) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: what the server says a delete left, shown whole and replaced whole; its entries never move
              <li key={i}>{String(path)}</li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

// A failure as an ApiError. Anything else is a fault in the page, which is
// reported (clientErrors.ts) and shown as an answer the page does not understand.
export function asApiError(error: unknown): ApiError {
  if (error instanceof ApiError) return error;
  noteFailure({ kind: PAGE_ERROR, cause: error });
  return new ApiError(0, UNEXPECTED_RESPONSE, m.unexpectedResponse);
}

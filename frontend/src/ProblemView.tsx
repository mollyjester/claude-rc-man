import { ApiError } from "./api";
import { m } from "./messages";

// Shows an error exactly as the server described it.
export function ProblemView({ error }: { error: ApiError | null }) {
  if (!error) return null;
  const screen = error.extra.screen;
  const failed = error.extra.failed;
  return (
    <div role="alert" className="problem">
      <p>{error.title}</p>
      {error.errors.length > 0 && (
        <ul aria-label={m.problemFieldErrors}>
          {error.errors.map((e, i) => (
            <li key={i}>{`${e.field}: ${e.message}`}</li>
          ))}
        </ul>
      )}
      {Array.isArray(failed) && (
        <p>
          {m.versionsFailedChecks} {failed.join(", ")}
        </p>
      )}
      {Array.isArray(screen) && <pre className="screen">{screen.join("\n")}</pre>}
    </div>
  );
}

export function asApiError(error: unknown): ApiError {
  return error instanceof ApiError ? error : new ApiError(0, "unexpected_response", m.unexpectedResponse);
}

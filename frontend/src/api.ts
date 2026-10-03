// The one way the frontend talks to the backend. Every failure becomes an
// ApiError carrying the server's problem details as they were sent.
import { m } from "./messages";

export type FieldError = { field: string; message: string };

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    public title: string,
    public errors: FieldError[] = [],
    public extra: Record<string, unknown> = {},
  ) {
    super(title);
  }
}

type Query = Record<string, string | number | boolean | undefined>;

// Problem details as the server sent them, in a failed response or in an
// answer that reports one, as an ApiError.
export function problemFrom(data: unknown, status: number): ApiError {
  const { status: _status, code, title, errors, type: _type, ...extra } = (data ?? {}) as Record<string, unknown>;
  void _status;
  void _type;
  return new ApiError(
    status,
    typeof code === "string" ? code : "unexpected_response",
    typeof title === "string" ? title : m.unexpectedResponse,
    Array.isArray(errors) ? (errors as FieldError[]) : [],
    extra,
  );
}

async function request<T>(method: string, path: string, body?: unknown, query?: Query): Promise<T> {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined && value !== "") params.set(key, String(value));
  }
  const url = params.size ? `${path}?${params}` : path;
  const response = await fetch(url, {
    method,
    credentials: "same-origin",
    headers: body === undefined ? {} : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  let data: Record<string, unknown> | undefined;
  try {
    data = text ? JSON.parse(text) : undefined;
  } catch {
    data = undefined;
  }
  if (!response.ok) throw problemFrom(data, response.status);
  return data as T;
}

export const api = {
  get: <T>(path: string, query?: Query) => request<T>("GET", path, undefined, query),
  post: <T>(path: string, body: unknown = {}) => request<T>("POST", path, body),
  patch: <T>(path: string, body: unknown) => request<T>("PATCH", path, body),
  delete: <T>(path: string, query?: Query) => request<T>("DELETE", path, undefined, query),
};

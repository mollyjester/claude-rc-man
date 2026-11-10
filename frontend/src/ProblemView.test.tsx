import { render, screen } from "@testing-library/react";

import { ApiError, SIGNED_OUT, UNEXPECTED_RESPONSE } from "./api";
import { PAGE_ERROR, takeReports } from "./clientErrors";
import { m } from "./messages";
import { asApiError, ProblemView } from "./ProblemView";

test("shows the server's title and every field error exactly as sent", () => {
  const error = new ApiError(422, "validation_failed", "Some of what was sent is not valid.", [
    { field: "prefix", message: "planted reason, as the server sent it" },
    { field: "author_name", message: `planted reason holding < and >, "quotes" and 'apostrophes'` },
  ]);
  render(<ProblemView error={error} />);
  expect(screen.getByRole("alert")).toHaveTextContent("Some of what was sent is not valid.");
  expect(screen.getByText("prefix: planted reason, as the server sent it")).toBeInTheDocument();
  expect(
    screen.getByText(`author_name: planted reason holding < and >, "quotes" and 'apostrophes'`),
  ).toBeInTheDocument();
});

test("an ended sign-in offers to sign in again and come back to the page", () => {
  const before = window.location.href;
  window.history.pushState({}, "", "/settings?x=1");
  try {
    render(<ProblemView error={new ApiError(0, SIGNED_OUT, m.signedOut)} />);
    expect(screen.getByRole("alert")).toHaveTextContent(m.signedOut);
    expect(screen.getByRole("link", { name: m.signInAgain })).toHaveAttribute(
      "href",
      "/oauth2/start?rd=%2Fsettings%3Fx%3D1",
    );
  } finally {
    window.history.replaceState({}, "", before);
  }
});

test("a fault in the page is not taken for rcman unreachable", () => {
  const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
  try {
    // What a browser's fetch throws when rcman is not reached, thrown by the page itself.
    const fault = new TypeError("Failed to fetch");
    const error = asApiError(fault);
    expect([error.code, error.title]).toEqual([UNEXPECTED_RESPONSE, m.unexpectedResponse]);
    expect(takeReports()).toEqual([
      expect.objectContaining({ kind: PAGE_ERROR, name: "TypeError", method: null, path: null }),
    ]);
    expect(consoleError).toHaveBeenCalledWith("rcman:", PAGE_ERROR, undefined, undefined, fault);
    const sent = new ApiError(409, "folder_busy", "Busy.");
    expect(asApiError(sent)).toBe(sent);
    expect(takeReports()).toEqual([]); // a problem rcman sent is not reported
  } finally {
    consoleError.mockRestore();
  }
});

import { render, screen, within } from "@testing-library/react";

import { ApiError } from "./api";
import { m } from "./messages";
import { ProblemView } from "./ProblemView";

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

test("names a field by the form's label when given one, and by its name otherwise", () => {
  const error = new ApiError(422, "validation_failed", "Some of what was sent is not valid.", [
    { field: "prefix", message: "planted reason" },
    { field: "request", message: "planted reason about the whole request" },
  ]);
  render(<ProblemView error={error} labels={{ prefix: "Planted label" }} />);
  expect(screen.getByText("Planted label: planted reason")).toBeInTheDocument();
  expect(screen.queryByText("prefix: planted reason")).not.toBeInTheDocument();
  expect(screen.getByText("request: planted reason about the whole request")).toBeInTheDocument();
});

test("shows a raw screen the server passed on", () => {
  const error = new ApiError(502, "claude_unrecognised_output", "Unrecognised.", [], { screen: ["line one", "line two"] });
  render(<ProblemView error={error} />);
  expect(screen.getByText(/line one\s+line two/)).toBeInTheDocument();
});

test("shows the server a problem names, with its screen", () => {
  const server = { path: "projects/alpha", state: "unrecognised", screen: ["still here", "and here"] };
  const error = new ApiError(409, "delete_stop_failed", "A server did not stop.", [], { server, stopped: [] });
  render(<ProblemView error={error} />);
  const alert = screen.getByRole("alert");
  expect(alert).toHaveTextContent("A server did not stop.");
  expect(alert).toHaveTextContent(`${m.problemServer} projects/alpha`);
  expect(screen.getByText(/still here\s+and here/)).toBeInTheDocument();
});

test("lists what a problem says was left", () => {
  const left = ["projects/beta", "projects/beta/stuck"];
  const error = new ApiError(409, "delete_incomplete", "Some was left.", [], { left, forgotten: [] });
  render(<ProblemView error={error} />);
  const list = screen.getByRole("list", { name: m.problemLeft });
  expect(within(list).getAllByRole("listitem").map((li) => li.textContent)).toEqual(left);
});

test("shows nothing of a server or what was left when the problem names neither", () => {
  render(<ProblemView error={new ApiError(409, "folder_busy", "Busy.")} />);
  expect(screen.getByRole("alert")).toHaveTextContent("Busy."); // positive control
  expect(screen.queryByText(m.problemServer, { exact: false })).not.toBeInTheDocument();
  expect(screen.queryByRole("list")).not.toBeInTheDocument();
});

test("shows nothing without an error", () => {
  const { container } = render(<ProblemView error={null} />);
  expect(container).toBeEmptyDOMElement();
});

test("shows what git said when it refused, and how much more it said", () => {
  const git = ["error: Your local changes would be overwritten:", "\ta.txt", "Aborting"];
  const error = new ApiError(409, "git_failed", "git refused.", [], { git, git_more: 4 });
  render(<ProblemView error={error} />);
  const said = screen.getByLabelText(m.gitSaid);
  expect(said.tagName).toBe("PRE");
  expect(said.textContent).toBe(git.join("\n"));
  expect(screen.getByRole("alert")).toHaveTextContent(m.andMoreLines(4));
});

test("says nothing of more lines when git said no more", () => {
  render(<ProblemView error={new ApiError(409, "git_failed", "git refused.", [], { git: ["one"], git_more: 0 })} />);
  expect(screen.getByLabelText(m.gitSaid)).toHaveTextContent("one"); // positive control
  expect(screen.getByRole("alert")).not.toHaveTextContent(m.andMoreLines(0));
});

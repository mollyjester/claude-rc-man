import { render, screen } from "@testing-library/react";

import { ApiError } from "./api";
import { ProblemView } from "./ProblemView";

test("shows the server's title and every field error exactly as sent", () => {
  const error = new ApiError(422, "validation_failed", "Some of what was sent is not valid.", [
    { field: "prefix", message: "String should match pattern '^x$'" },
    { field: "email", message: "Field required" },
  ]);
  render(<ProblemView error={error} />);
  expect(screen.getByRole("alert")).toHaveTextContent("Some of what was sent is not valid.");
  expect(screen.getByText("prefix: String should match pattern '^x$'")).toBeInTheDocument();
  expect(screen.getByText("email: Field required")).toBeInTheDocument();
});

test("shows a raw screen the server passed on", () => {
  const error = new ApiError(502, "claude_unrecognised_output", "Unrecognised.", [], { screen: ["line one", "line two"] });
  render(<ProblemView error={error} />);
  expect(screen.getByText(/line one\s+line two/)).toBeInTheDocument();
});

test("shows nothing without an error", () => {
  const { container } = render(<ProblemView error={null} />);
  expect(container).toBeEmptyDOMElement();
});

import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { m } from "./messages";
import { CONNECTED, ME, fakeApi, problem } from "./test/fakeApi";
import { renderApp } from "./test/render";

test("the prefix is shown and saved", async () => {
  const fake = fakeApi({
    "GET /api/me": ME,
    "GET /api/claude": CONNECTED,
    "GET /api/me/settings": { prefix: "alice" },
    "PUT /api/me/settings": (c) => c.body,
  });
  renderApp("/settings");
  const input = await screen.findByLabelText(m.prefixLabel);
  await waitFor(() => expect(input).toHaveValue("alice"));
  await userEvent.clear(input);
  await userEvent.type(input, "box-1");
  await userEvent.click(screen.getByRole("button", { name: m.save }));
  await waitFor(() => expect(fake.made("PUT", "/api/me/settings")[0].body).toEqual({ prefix: "box-1" }));
  expect(await screen.findByText(m.saved)).toBeInTheDocument();
});

test("what the server refuses is shown as it said it", async () => {
  fakeApi({
    "GET /api/me": ME,
    "GET /api/claude": CONNECTED,
    "GET /api/me/settings": { prefix: "alice" },
    "PUT /api/me/settings": problem(422, "validation_failed", "Some of what was sent is not valid.", {
      errors: [{ field: "prefix", message: "String should match pattern" }],
    }),
  });
  renderApp("/settings");
  const input = await screen.findByLabelText(m.prefixLabel);
  await userEvent.clear(input);
  await userEvent.click(screen.getByRole("button", { name: m.save }));
  expect(await screen.findByText("prefix: String should match pattern")).toBeInTheDocument();
});

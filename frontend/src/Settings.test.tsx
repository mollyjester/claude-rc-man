import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { m } from "./messages";
import { FIELDS } from "./Settings";
import { CONNECTED, ME, type Routes, SETTINGS, fakeApi, problem } from "./test/fakeApi";
import { renderApp } from "./test/render";

const AUTHOR = { name: "Alice A", email: "alice@example.com" };

function api(routes: Routes = {}) {
  return fakeApi({
    "GET /api/me": ME,
    "GET /api/claude": CONNECTED,
    "GET /api/me/settings": SETTINGS,
    "PATCH /api/me/settings": (c) => ({ ...SETTINGS, ...(c.body as object) }),
    ...routes,
  });
}

const part = (title: string) => screen.findByRole("region", { name: title });

test("both parts are filled in from the server, which alone says who the author is", async () => {
  api({ "GET /api/me/settings": { ...SETTINGS, author_name: "Alice A", author_email: "a@work.example" } });
  renderApp("/settings");
  const prefix = await part(m.settingsPrefixTitle);
  const author = await part(m.settingsAuthorTitle);
  await waitFor(() => expect(within(prefix).getByLabelText(FIELDS.prefix.label)).toHaveValue("alice"));
  expect(within(author).getByLabelText(FIELDS.author_name.label)).toHaveValue("Alice A");
  expect(within(author).getByLabelText(FIELDS.author_email.label)).toHaveValue("a@work.example");
  expect(within(author).getByText(m.authorNotSet)).toBeInTheDocument(); // as the server said: no effective author
});

test("the effective author is the one the server reports", async () => {
  api({ "GET /api/me/settings": { ...SETTINGS, author_name: "Alice A", effective_author: AUTHOR } });
  renderApp("/settings");
  const author = await part(m.settingsAuthorTitle);
  expect(await within(author).findByText(m.authorEffective(AUTHOR.name, AUTHOR.email))).toBeInTheDocument();
  expect(within(author).queryByText(m.authorNotSet)).toBeNull();
});

test("saving the prefix sends only the prefix", async () => {
  const fake = api();
  renderApp("/settings");
  const prefix = await part(m.settingsPrefixTitle);
  const input = within(prefix).getByLabelText(FIELDS.prefix.label);
  await waitFor(() => expect(input).toHaveValue("alice"));
  await userEvent.clear(input);
  await userEvent.type(input, "box-1");
  await userEvent.click(within(prefix).getByRole("button", { name: m.save }));
  await waitFor(() => expect(fake.made("PATCH", "/api/me/settings")[0]?.body).toEqual({ prefix: "box-1" }));
  expect(await within(prefix).findByText(m.saved)).toBeInTheDocument();
});

test("saving the author sends the name and the email as typed, and shows what the server answers", async () => {
  const fake = api({
    "PATCH /api/me/settings": (c) => ({ ...SETTINGS, ...(c.body as object), effective_author: AUTHOR }),
  });
  renderApp("/settings");
  const author = await part(m.settingsAuthorTitle);
  await userEvent.type(within(author).getByLabelText(FIELDS.author_name.label), "Alice A");
  await userEvent.click(within(author).getByRole("button", { name: m.save }));
  await waitFor(() =>
    expect(fake.made("PATCH", "/api/me/settings")[0]?.body).toEqual({ author_name: "Alice A", author_email: "" }),
  );
  expect(await within(author).findByText(m.authorEffective(AUTHOR.name, AUTHOR.email))).toBeInTheDocument();
  expect(within(author).getByText(m.saved)).toBeInTheDocument();
});

test("what the server refuses is shown as it said it, in the part that sent it", async () => {
  api({
    "PATCH /api/me/settings": problem(422, "validation_failed", "Some of what was sent is not valid.", {
      errors: [{ field: "author_name", message: "String should match pattern" }],
    }),
  });
  renderApp("/settings");
  const author = await part(m.settingsAuthorTitle);
  const prefix = await part(m.settingsPrefixTitle);
  await userEvent.type(within(author).getByLabelText(FIELDS.author_name.label), "<b>");
  await userEvent.click(within(author).getByRole("button", { name: m.save }));
  expect(await within(author).findByText("author_name: String should match pattern")).toBeInTheDocument();
  expect(within(prefix).queryByRole("alert")).toBeNull();
});

test("saving one part leaves the other part's status alone", async () => {
  let refuse = false;
  api({
    "PATCH /api/me/settings": (c) =>
      refuse
        ? problem(422, "validation_failed", "Some of what was sent is not valid.", {
            errors: [{ field: "author_name", message: "String should match pattern" }],
          })
        : { ...SETTINGS, ...(c.body as object) },
  });
  renderApp("/settings");
  const prefix = await part(m.settingsPrefixTitle);
  const author = await part(m.settingsAuthorTitle);
  await waitFor(() => expect(within(prefix).getByLabelText(FIELDS.prefix.label)).toHaveValue("alice"));
  await userEvent.click(within(prefix).getByRole("button", { name: m.save }));
  expect(await within(prefix).findByText(m.saved)).toBeInTheDocument();
  expect(within(author).queryByText(m.saved)).toBeNull();
  refuse = true;
  await userEvent.click(within(author).getByRole("button", { name: m.save }));
  expect(await within(author).findByRole("alert")).toBeInTheDocument();
  expect(within(prefix).getByText(m.saved)).toBeInTheDocument();
  expect(within(prefix).queryByRole("alert")).toBeNull();
});

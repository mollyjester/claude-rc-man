import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { m } from "./messages";
import { FIELDS } from "./Settings";
import { fakeApi, ME, type Routes, SETTINGS } from "./test/fakeApi";
import { renderApp } from "./test/render";

const AUTHOR = { name: "Alice A", email: "alice@example.com" };

function api(routes: Routes = {}) {
  return fakeApi({
    "GET /api/me": ME,
    "GET /api/me/settings": SETTINGS,
    "PATCH /api/me/settings": (c) => ({ ...SETTINGS, ...(c.body as object) }),
    "GET /api/github": { token: false },
    ...routes,
  });
}

const part = (title: string) => screen.findByRole("region", { name: title });

test("both parts are filled in from the server, which alone says who the author is", async () => {
  api({
    "GET /api/me/settings": {
      ...SETTINGS,
      prefix: "alice",
      author_name: "Alice A",
      author_email: "a@work.example",
    },
  });
  renderApp("/settings");
  const prefix = await part(m.settingsPrefixTitle);
  const author = await part(m.settingsAuthorTitle);
  await waitFor(() =>
    expect(within(prefix).getByLabelText(FIELDS.prefix.label)).toHaveValue("alice"),
  );
  expect(within(author).getByLabelText(FIELDS.author_name.label)).toHaveValue("Alice A");
  expect(within(author).getByLabelText(FIELDS.author_email.label)).toHaveValue("a@work.example");
  expect(within(author).getByText(m.authorNotSet)).toBeInTheDocument(); // as the server said: no effective author
});

test("saving the prefix sends only the prefix", async () => {
  const fake = api({ "GET /api/me/settings": { ...SETTINGS, prefix: "alice" } });
  renderApp("/settings");
  const prefix = await part(m.settingsPrefixTitle);
  const input = within(prefix).getByLabelText(FIELDS.prefix.label);
  await waitFor(() => expect(input).toHaveValue("alice"));
  await userEvent.clear(input);
  await userEvent.type(input, "box-1");
  await userEvent.click(within(prefix).getByRole("button", { name: m.save }));
  await waitFor(() =>
    expect(fake.made("PATCH", "/api/me/settings")[0]?.body).toEqual({ prefix: "box-1" }),
  );
  expect(await within(prefix).findByText(m.saved)).toBeInTheDocument();
});

test("saving the author sends the name and the email as typed, and shows what the server answers", async () => {
  const fake = api({
    "PATCH /api/me/settings": (c) => ({
      ...SETTINGS,
      ...(c.body as object),
      effective_author: AUTHOR,
    }),
  });
  renderApp("/settings");
  const author = await part(m.settingsAuthorTitle);
  await userEvent.type(within(author).getByLabelText(FIELDS.author_name.label), "Alice A");
  await userEvent.click(within(author).getByRole("button", { name: m.save }));
  await waitFor(() =>
    expect(fake.made("PATCH", "/api/me/settings")[0]?.body).toEqual({
      author_name: "Alice A",
      author_email: "",
    }),
  );
  expect(
    await within(author).findByText(m.authorEffective(AUTHOR.name, AUTHOR.email)),
  ).toBeInTheDocument();
  expect(within(author).getByText(m.saved)).toBeInTheDocument();
});

// --- what the page offers ------------------------------------------------------

const PLANTED = "planted-secret-token-in-the-reply";

test("the GitHub token part says whether one is stored, and never fills its field from the server", async () => {
  api({ "GET /api/github": { token: true, value: PLANTED } });
  renderApp("/settings");
  const github = await part(m.settingsGithubTitle);
  expect(await within(github).findByText(m.githubTokenStored)).toBeInTheDocument();
  const field = within(github).getByLabelText(m.githubTokenLabel);
  expect(field).toHaveAttribute("type", "password");
  expect(field).toHaveAttribute("autocomplete", "new-password");
  expect(field).toHaveValue("");
  expect(document.body.textContent).not.toContain(PLANTED);
  for (const input of document.querySelectorAll("input"))
    expect(input.value).not.toContain(PLANTED);
});

test("Save sends the token as typed, then clears the field and says it is stored", async () => {
  const fake = api({ "POST /api/github/token": { token: true } });
  renderApp("/settings");
  const github = await part(m.settingsGithubTitle);
  await within(github).findByText(m.githubTokenNotStored);
  const field = within(github).getByLabelText(m.githubTokenLabel);
  await userEvent.type(field, "ghp_typed-token");
  const save = within(github).getByRole("button", { name: m.save });
  expect(save).toHaveClass("primary");
  await userEvent.click(save);
  await waitFor(() => expect(fake.made("POST", "/api/github/token")).toHaveLength(1));
  expect(fake.made("POST", "/api/github/token")[0].body).toEqual({ token: "ghp_typed-token" });
  expect(await within(github).findByText(m.githubTokenSaved)).toBeInTheDocument();
  expect(field).toHaveValue("");
  expect(within(github).getByText(m.githubTokenStored)).toBeInTheDocument();
});

test("Remove asks first, then removes the token", async () => {
  const fake = api({
    "GET /api/github": { token: true },
    "DELETE /api/github/token": { token: false },
  });
  renderApp("/settings");
  const github = await part(m.settingsGithubTitle);
  const remove = await within(github).findByRole("button", { name: m.githubTokenRemove });
  await userEvent.click(remove);
  const dialog = await screen.findByRole("dialog", { name: m.githubTokenRemoveTitle });
  expect(fake.made("DELETE", "/api/github/token")).toHaveLength(0); // nothing yet
  await userEvent.click(within(dialog).getByRole("button", { name: m.githubTokenRemoveConfirm }));
  await waitFor(() => expect(fake.made("DELETE", "/api/github/token")).toHaveLength(1));
  expect(await within(github).findByText(m.githubTokenNotStored)).toBeInTheDocument();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(
    within(github).queryByRole("button", { name: m.githubTokenRemove }),
  ).not.toBeInTheDocument();
});

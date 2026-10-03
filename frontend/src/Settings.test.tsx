import { act, screen, waitFor, within } from "@testing-library/react";
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
    "GET /api/github": { token: false },
    ...routes,
  });
}

const part = (title: string) => screen.findByRole("region", { name: title });

test("both parts are filled in from the server, which alone says who the author is", async () => {
  api({
    "GET /api/me/settings": { ...SETTINGS, author_name: "Alice A", author_email: "a@work.example" },
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

test("the effective author is the one the server reports", async () => {
  api({
    "GET /api/me/settings": { ...SETTINGS, author_name: "Alice A", effective_author: AUTHOR },
  });
  renderApp("/settings");
  const author = await part(m.settingsAuthorTitle);
  expect(
    await within(author).findByText(m.authorEffective(AUTHOR.name, AUTHOR.email)),
  ).toBeInTheDocument();
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

test("what the server refuses is shown as it said it, in the part that sent it", async () => {
  api({
    "PATCH /api/me/settings": problem(
      422,
      "validation_failed",
      "Some of what was sent is not valid.",
      {
        errors: [{ field: "author_name", message: "planted reason, as the server sent it" }],
      },
    ),
  });
  renderApp("/settings");
  const author = await part(m.settingsAuthorTitle);
  const prefix = await part(m.settingsPrefixTitle);
  await userEvent.type(within(author).getByLabelText(FIELDS.author_name.label), "<b>");
  await userEvent.click(within(author).getByRole("button", { name: m.save }));
  expect(
    await within(author).findByText(
      `${FIELDS.author_name.label}: planted reason, as the server sent it`,
    ),
  ).toBeInTheDocument();
  expect(within(prefix).queryByRole("alert")).toBeNull();
});

test("saving one part leaves the other part's status alone", async () => {
  let refuse = false;
  api({
    "PATCH /api/me/settings": (c) =>
      refuse
        ? problem(422, "validation_failed", "Some of what was sent is not valid.", {
            errors: [{ field: "author_name", message: "planted reason, as the server sent it" }],
          })
        : { ...SETTINGS, ...(c.body as object) },
  });
  renderApp("/settings");
  const prefix = await part(m.settingsPrefixTitle);
  const author = await part(m.settingsAuthorTitle);
  await waitFor(() =>
    expect(within(prefix).getByLabelText(FIELDS.prefix.label)).toHaveValue("alice"),
  );
  await userEvent.click(within(prefix).getByRole("button", { name: m.save }));
  expect(await within(prefix).findByText(m.saved)).toBeInTheDocument();
  expect(within(author).queryByText(m.saved)).toBeNull();
  refuse = true;
  await userEvent.click(within(author).getByRole("button", { name: m.save }));
  expect(await within(author).findByRole("alert")).toBeInTheDocument();
  expect(within(prefix).getByText(m.saved)).toBeInTheDocument();
  expect(within(prefix).queryByRole("alert")).toBeNull();
});

// --- the GitHub token -----------------------------------------------------------

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

test("with no token stored the part says so, and offers no Remove", async () => {
  api();
  renderApp("/settings");
  const github = await part(m.settingsGithubTitle);
  expect(await within(github).findByText(m.githubTokenNotStored)).toBeInTheDocument();
  expect(
    within(github).queryByRole("button", { name: m.githubTokenRemove }),
  ).not.toBeInTheDocument();
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

test("a refused token stays in the field, and the refusal is shown by its label", async () => {
  api({
    "POST /api/github/token": problem(
      422,
      "validation_failed",
      "Some of what was sent is not valid.",
      {
        errors: [{ field: "token", message: "planted reason, as the server sent it" }],
      },
    ),
  });
  renderApp("/settings");
  const github = await part(m.settingsGithubTitle);
  const field = await within(github).findByLabelText(m.githubTokenLabel);
  await userEvent.type(field, "not a token");
  await userEvent.click(within(github).getByRole("button", { name: m.save }));
  expect(
    await within(github).findByText(`${m.githubTokenLabel}: planted reason, as the server sent it`),
  ).toBeInTheDocument();
  expect(field).toHaveValue("not a token");
  expect(within(github).queryByText(m.githubTokenSaved)).toBeNull();
});

test("Remove asks first, then removes the token", async () => {
  const fake = api({
    "GET /api/github": { token: true },
    "DELETE /api/github/token": { token: false },
  });
  renderApp("/settings");
  const github = await part(m.settingsGithubTitle);
  const remove = await within(github).findByRole("button", { name: m.githubTokenRemove });
  expect(remove).toHaveClass("secondary-danger");
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

test("Cancel leaves the token alone", async () => {
  const fake = api({ "GET /api/github": { token: true } });
  renderApp("/settings");
  const github = await part(m.settingsGithubTitle);
  await userEvent.click(await within(github).findByRole("button", { name: m.githubTokenRemove }));
  const dialog = await screen.findByRole("dialog", { name: m.githubTokenRemoveTitle });
  await userEvent.click(within(dialog).getByRole("button", { name: m.cancel }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(fake.made("DELETE", "/api/github/token")).toHaveLength(0);
  expect(within(github).getByText(m.githubTokenStored)).toBeInTheDocument();
});

test("what was saved is no longer said once Remove is asked for", async () => {
  api({ "POST /api/github/token": { token: true } });
  renderApp("/settings");
  const github = await part(m.settingsGithubTitle);
  await userEvent.type(await within(github).findByLabelText(m.githubTokenLabel), "ghp_typed-token");
  await userEvent.click(within(github).getByRole("button", { name: m.save }));
  expect(await within(github).findByText(m.githubTokenSaved)).toBeInTheDocument();
  await userEvent.click(within(github).getByRole("button", { name: m.githubTokenRemove }));
  await screen.findByRole("dialog", { name: m.githubTokenRemoveTitle });
  expect(within(github).queryByText(m.githubTokenSaved)).not.toBeInTheDocument();
});

test("the token's help says what git uses it for, as docs/git.md does: pushing, cloning a private repository, and in Claude sessions", () => {
  expect(m.githubTokenHelp).toMatch(/push to GitHub/);
  expect(m.githubTokenHelp).toMatch(/clone private repositories/);
  expect(m.githubTokenHelp).toMatch(/Claude sessions/);
});

test("the token field has its help below it, as every other field", async () => {
  api();
  renderApp("/settings");
  const github = await part(m.settingsGithubTitle);
  const label = (await within(github).findByLabelText(m.githubTokenLabel)).closest("label")!;
  expect(label.nextElementSibling).toHaveTextContent(m.githubTokenHelp);
  const prefix = (await part(m.settingsPrefixTitle)).querySelector("label")!;
  expect(label.parentElement!.tagName).toBe(prefix.parentElement!.tagName);
  expect(label.nextElementSibling!.className).toBe(prefix.nextElementSibling!.className);
});

test("until the server says whether a token is stored, the part claims neither and offers no Remove", async () => {
  let answer: (value: unknown) => void = () => {};
  api({ "GET /api/github": () => new Promise((resolve) => (answer = resolve)) });
  renderApp("/settings");
  const github = await part(m.settingsGithubTitle);
  expect(within(github).getByLabelText(m.githubTokenLabel)).toBeInTheDocument(); // positive control
  expect(within(github).queryByText(m.githubTokenStored)).not.toBeInTheDocument();
  expect(within(github).queryByText(m.githubTokenNotStored)).not.toBeInTheDocument();
  expect(
    within(github).queryByRole("button", { name: m.githubTokenRemove }),
  ).not.toBeInTheDocument();
  await act(async () => answer({ token: true }));
  expect(
    await within(github).findByRole("button", { name: m.githubTokenRemove }),
  ).toBeInTheDocument();
});

test("when the server cannot say whether a token is stored, the part shows why and offers no Remove", async () => {
  api({ "GET /api/github": problem(504, "helper_timeout", "planted: the helper took too long") });
  renderApp("/settings");
  const github = await part(m.settingsGithubTitle);
  expect(await within(github).findByText("planted: the helper took too long")).toBeInTheDocument();
  expect(within(github).queryByText(m.githubTokenStored)).not.toBeInTheDocument();
  expect(within(github).queryByText(m.githubTokenNotStored)).not.toBeInTheDocument();
  expect(
    within(github).queryByRole("button", { name: m.githubTokenRemove }),
  ).not.toBeInTheDocument();
});

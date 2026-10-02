import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

import { m } from "./messages";
import { CONNECTED, ME, type Routes, fakeApi, problem } from "./test/fakeApi";
import { renderApp } from "./test/render";

const REPO = "projects/repo";
const entry = (path: string, repository = false) => ({ name: path.split("/").pop(), path, has_children: false, repository });
const FOLDERS: Record<string, unknown> = {
  "": { path: "", entries: [{ ...entry("projects"), has_children: true }] },
  projects: { path: "projects", entries: [entry(REPO, true), entry("projects/plain")] },
};

type Files = { path: string; change: string; from?: string; submodule?: boolean; selectable?: boolean }[];

function status(staged: Files = [], notStaged: Files = [], extra: Record<string, unknown> = {}) {
  return {
    path: REPO,
    branch: { name: "main", commit: "0123456789abcdef", upstream: "origin/main", ahead: 1, behind: 2 },
    staged: { files: staged, more: 0 },
    not_staged: { files: notStaged, more: 0 },
    author: { name: "Alice", email: "alice@example.com" },
    ...extra,
  };
}

const BRANCHES = {
  path: REPO,
  local: {
    branches: [
      { name: "main", current: true, upstream: "origin/main" },
      { name: "topic", current: false, upstream: null },
    ],
    more: 0,
  },
  remote: {
    branches: [
      { name: "origin/main", local: "main" },
      { name: "origin/feature", local: null },
    ],
    more: 0,
  },
};

function repoApi(routes: Routes = {}) {
  return fakeApi({
    "GET /api/me": ME,
    "GET /api/claude": CONNECTED,
    "GET /api/servers": { servers: [] },
    "GET /api/folders": (c) => FOLDERS[c.query.get("path") ?? ""] ?? problem(404, "path_not_found", "No."),
    "GET /api/git/status": status([], [{ path: "new.txt", change: "untracked" }]),
    "GET /api/git/branches": BRANCHES,
    ...routes,
  });
}

const changes = async () => within(await screen.findByRole("region", { name: m.changesTitle }));
const list = async (title: string) => within(await (await changes()).findByRole("group", { name: title }));

test("a repository's panel has a Changes section", async () => {
  const fake = repoApi();
  renderApp(`/?folder=${REPO}`);
  expect(await changes()).toBeTruthy();
  await waitFor(() => expect(fake.made("GET", "/api/git/branches").length).toBeGreaterThan(0));
  expect(fake.made("GET", "/api/git/status")[0].query.get("path")).toBe(REPO);
  expect(fake.made("GET", "/api/git/branches")[0].query.get("path")).toBe(REPO);
});

test("the panel of a folder that is not a repository has no Changes section, and asks git nothing", async () => {
  const fake = repoApi();
  renderApp("/?folder=projects/plain");
  const panel = within(await screen.findByRole("region", { name: "projects/plain" }));
  await panel.findByRole("heading", { name: m.serverTitle }); // positive control: the panel is there
  await waitFor(() => expect(fake.made("GET", "/api/folders").length).toBeGreaterThanOrEqual(2));
  expect(panel.queryByRole("region", { name: m.changesTitle })).not.toBeInTheDocument();
  expect(fake.calls.filter((c) => c.path.startsWith("/api/git/"))).toHaveLength(0);
});

test("each file is listed with its change and its name as it is", async () => {
  const names = ["with space.txt", "with\nnewline.txt"];
  repoApi({
    "GET /api/git/status": {
      ...status(
        [{ path: "moved.txt", change: "renamed", from: "old.txt" }],
        [
          ...names.map((path) => ({ path, change: "modified" })),
          { path: "lib", change: "modified", submodule: true },
          { path: "caf�.txt", change: "untracked", selectable: false },
          { path: "x", change: "brand_new_kind" },
        ],
      ),
      not_staged: {
        files: [
          ...names.map((path) => ({ path, change: "modified" })),
          { path: "caf�.txt", change: "untracked", selectable: false },
          { path: "x", change: "brand_new_kind" },
        ],
        more: 4,
      },
    },
  });
  renderApp(`/?folder=${REPO}`);
  const staged = await list(m.stagedTitle);
  expect(staged.getByRole("checkbox", { name: /moved\.txt/ })).toBeInTheDocument();
  expect(staged.getByText(m.changes.renamed, { exact: false })).toBeInTheDocument();
  expect(staged.getByText(m.renamedFrom("old.txt"))).toBeInTheDocument();
  const notStaged = await list(m.notStagedTitle);
  for (const name of names) {
    const box = notStaged.getByRole("checkbox", { name: (n) => n.includes(name) });
    expect(box.closest("li")!.textContent).toContain(name);
  }
  expect(notStaged.getByText("brand_new_kind", { exact: false })).toBeInTheDocument();
  expect(notStaged.getByText(m.andMore(4))).toBeInTheDocument();
  const odd = notStaged.getByText("caf�.txt", { exact: false }).closest("li")!;
  expect(within(odd).queryByRole("checkbox")).not.toBeInTheDocument();
  expect(odd).toHaveTextContent(m.notSelectable);
});

test("Stage selected sends exactly the files checked, then lists again", async () => {
  let staged: Files = [];
  const fake = repoApi({
    "GET /api/git/status": () =>
      status(staged, [
        { path: "a.txt", change: "untracked" },
        { path: "b.txt", change: "modified" },
        { path: "c.txt", change: "deleted" },
      ].filter((f) => !staged.some((s) => s.path === f.path))),
    "POST /api/git/stage": (c) => {
      staged = (c.body as { files: string[] }).files.map((path) => ({ path, change: "added" }));
      return { path: REPO };
    },
  });
  renderApp(`/?folder=${REPO}`);
  const notStaged = await list(m.notStagedTitle);
  const button = notStaged.getByRole("button", { name: m.stageSelected });
  expect(button).toBeDisabled();
  await userEvent.click(notStaged.getByRole("checkbox", { name: /a\.txt/ }));
  await userEvent.click(notStaged.getByRole("checkbox", { name: /c\.txt/ }));
  expect(button).toBeEnabled();
  const before = fake.made("GET", "/api/git/status").length;
  await userEvent.click(button);
  await waitFor(() => expect(fake.made("POST", "/api/git/stage")).toHaveLength(1));
  expect(fake.made("POST", "/api/git/stage")[0].body).toEqual({ path: REPO, files: ["a.txt", "c.txt"] });
  await waitFor(() => expect(fake.made("GET", "/api/git/status").length).toBeGreaterThan(before));
  const nowStaged = await list(m.stagedTitle);
  expect(await nowStaged.findByRole("checkbox", { name: /a\.txt/ })).toBeInTheDocument();
});

test("Unstage selected sends exactly the files checked", async () => {
  const fake = repoApi({
    "GET /api/git/status": status([
      { path: "a.txt", change: "added" },
      { path: "b.txt", change: "modified" },
    ]),
    "POST /api/git/unstage": { path: REPO },
  });
  renderApp(`/?folder=${REPO}`);
  const staged = await list(m.stagedTitle);
  const button = staged.getByRole("button", { name: m.unstageSelected });
  expect(button).toBeDisabled();
  await userEvent.click(staged.getByRole("checkbox", { name: /b\.txt/ }));
  await userEvent.click(button);
  await waitFor(() => expect(fake.made("POST", "/api/git/unstage")).toHaveLength(1));
  expect(fake.made("POST", "/api/git/unstage")[0].body).toEqual({ path: REPO, files: ["b.txt"] });
});

test("Select all checks every selectable file of its list, and again clears them", async () => {
  repoApi({
    "GET /api/git/status": status(
      [],
      [
        { path: "a.txt", change: "untracked" },
        { path: "b.txt", change: "untracked" },
        { path: "odd", change: "untracked", selectable: false },
      ],
    ),
  });
  renderApp(`/?folder=${REPO}`);
  const notStaged = await list(m.notStagedTitle);
  const all = notStaged.getByRole("checkbox", { name: m.selectAll });
  await userEvent.click(all);
  for (const name of [/a\.txt/, /b\.txt/]) expect(notStaged.getByRole("checkbox", { name })).toBeChecked();
  expect(all).toBeChecked();
  await userEvent.click(all);
  for (const name of [/a\.txt/, /b\.txt/]) expect(notStaged.getByRole("checkbox", { name })).not.toBeChecked();
});

test("what is checked survives a refresh, and a file no longer listed is dropped", async () => {
  let files: Files = [
    { path: "a.txt", change: "untracked" },
    { path: "b.txt", change: "untracked" },
  ];
  const fake = repoApi({
    "GET /api/git/status": () => status([], files),
    "POST /api/git/stage": { path: REPO },
  });
  renderApp(`/?folder=${REPO}`);
  const notStaged = await list(m.notStagedTitle);
  await userEvent.click(notStaged.getByRole("checkbox", { name: /a\.txt/ }));
  await userEvent.click(notStaged.getByRole("checkbox", { name: /b\.txt/ }));
  files = [{ path: "b.txt", change: "untracked" }, { path: "c.txt", change: "untracked" }];
  // a refresh: an action's, here a fetch of the branches does not count, so use Refresh's status
  await userEvent.click((await changes()).getByRole("button", { name: m.refreshStatus }));
  expect(await notStaged.findByRole("checkbox", { name: /c\.txt/ })).not.toBeChecked();
  expect(notStaged.getByRole("checkbox", { name: /b\.txt/ })).toBeChecked();
  await userEvent.click(notStaged.getByRole("button", { name: m.stageSelected }));
  await waitFor(() => expect(fake.made("POST", "/api/git/stage")).toHaveLength(1));
  expect(fake.made("POST", "/api/git/stage")[0].body).toEqual({ path: REPO, files: ["b.txt"] });
});

test("without a commit author, Commit is not offered and the section says where to set one", async () => {
  repoApi({ "GET /api/git/status": status([{ path: "a.txt", change: "added" }], [], { author: null }) });
  const app = renderApp(`/?folder=${REPO}`);
  const section = await changes();
  expect(await section.findByText(m.commitNeedsAuthor)).toBeInTheDocument();
  expect(section.queryByRole("button", { name: m.commit })).not.toBeInTheDocument();
  expect(section.queryByLabelText(m.commitMessage)).not.toBeInTheDocument();
  await userEvent.click(section.getByRole("link", { name: m.commitAuthorLink }));
  expect(app.location().pathname).toBe("/settings");
});

test("with nothing staged, Commit is not offered and the section says why", async () => {
  repoApi();
  renderApp(`/?folder=${REPO}`);
  const section = await changes();
  expect(await section.findByText(m.commitNothingStaged)).toBeInTheDocument();
  expect(section.queryByRole("button", { name: m.commit })).not.toBeInTheDocument();
});

test("Commit sends the message and empties the box", async () => {
  const fake = repoApi({
    "GET /api/git/status": status([{ path: "a.txt", change: "added" }]),
    "POST /api/git/commit": { path: REPO, commit: "abc", branch: "main" },
  });
  renderApp(`/?folder=${REPO}`);
  const box = await (await changes()).findByLabelText(m.commitMessage);
  await userEvent.type(box, "Add a.txt{enter}With a body");
  const commit = (await changes()).getByRole("button", { name: m.commit });
  expect(commit).toHaveClass("primary");
  await userEvent.click(commit);
  await waitFor(() => expect(fake.made("POST", "/api/git/commit")).toHaveLength(1));
  expect(fake.made("POST", "/api/git/commit")[0].body).toEqual({ path: REPO, message: "Add a.txt\nWith a body" });
  await waitFor(() => expect(box).toHaveValue(""));
});

test("an empty message is sent, and the server's refusal is shown by the box's label", async () => {
  const fake = repoApi({
    "GET /api/git/status": status([{ path: "a.txt", change: "added" }]),
    "POST /api/git/commit": problem(422, "validation_failed", "Some of what was sent is not valid.", {
      errors: [{ field: "message", message: "planted reason for the message" }],
    }),
  });
  renderApp(`/?folder=${REPO}`);
  await userEvent.click(await (await changes()).findByRole("button", { name: m.commit }));
  await waitFor(() => expect(fake.made("POST", "/api/git/commit")).toHaveLength(1));
  expect(fake.made("POST", "/api/git/commit")[0].body).toEqual({ path: REPO, message: "" });
  expect(await screen.findByText(`${m.commitMessage}: planted reason for the message`)).toBeInTheDocument();
});

test("the branch is shown with its upstream, or detached, or before the first commit", async () => {
  repoApi();
  renderApp(`/?folder=${REPO}`);
  expect(await (await changes()).findByText(m.onBranch("main"))).toBeInTheDocument();
  expect((await changes()).getByText(m.tracking("origin/main", 1, 2))).toBeInTheDocument();
});

test.each([
  [{ name: null, commit: "0123456789abcdef", upstream: null, ahead: null, behind: null }, () => m.detachedAt("0123456")],
  [{ name: "main", commit: null, upstream: null, ahead: null, behind: null }, () => m.noCommitsYet("main")],
])("a branch that is %o is shown as such", async (branch, text) => {
  repoApi({ "GET /api/git/status": status([], [], { branch }) });
  renderApp(`/?folder=${REPO}`);
  expect(await (await changes()).findByText(text())).toBeInTheDocument();
});

test("Switch is offered for other local branches and remote ones without a local branch", async () => {
  const fake = repoApi({ "POST /api/git/switch": { path: REPO, branch: "x" } });
  renderApp(`/?folder=${REPO}`);
  const local = within(await (await changes()).findByRole("list", { name: m.localBranches }));
  const remote = within((await changes()).getByRole("list", { name: m.remoteBranches }));
  expect(local.queryByRole("button", { name: m.switchTo("main") })).not.toBeInTheDocument();
  expect(remote.queryByRole("button", { name: m.switchTo("origin/main") })).not.toBeInTheDocument();
  await userEvent.click(local.getByRole("button", { name: m.switchTo("topic") }));
  await waitFor(() => expect(fake.made("POST", "/api/git/switch")).toHaveLength(1));
  expect(fake.made("POST", "/api/git/switch")[0].body).toEqual({ path: REPO, branch: "topic", remote: false });
  await userEvent.click(remote.getByRole("button", { name: m.switchTo("origin/feature") }));
  await waitFor(() => expect(fake.made("POST", "/api/git/switch")).toHaveLength(2));
  expect(fake.made("POST", "/api/git/switch")[1].body).toEqual({ path: REPO, branch: "origin/feature", remote: true });
});

test("New branch sends the name, and Refresh branches fetches and lists them again", async () => {
  const fake = repoApi({
    "POST /api/git/branch": { path: REPO, branch: "topic/new" },
    "POST /api/git/fetch": { path: REPO },
  });
  renderApp(`/?folder=${REPO}`);
  const section = await changes();
  await userEvent.type(await section.findByLabelText(m.newBranchName), "topic/new");
  await userEvent.click(section.getByRole("button", { name: m.createBranch }));
  await waitFor(() => expect(fake.made("POST", "/api/git/branch")).toHaveLength(1));
  expect(fake.made("POST", "/api/git/branch")[0].body).toEqual({ path: REPO, name: "topic/new" });
  await waitFor(() => expect(section.getByLabelText(m.newBranchName)).toHaveValue(""));
  const listed = fake.made("GET", "/api/git/branches").length;
  await userEvent.click(section.getByRole("button", { name: m.refreshBranches }));
  await waitFor(() => expect(fake.made("POST", "/api/git/fetch")).toHaveLength(1));
  expect(fake.made("POST", "/api/git/fetch")[0].body).toEqual({ path: REPO });
  await waitFor(() => expect(fake.made("GET", "/api/git/branches").length).toBeGreaterThan(listed));
  const fetched = fake.calls.findIndex((c) => c.path === "/api/git/fetch");
  expect(fake.calls.slice(fetched).some((c) => c.path === "/api/git/branches")).toBe(true);
});

test("git's refusal is shown with what it said", async () => {
  repoApi({
    "POST /api/git/switch": problem(409, "git_failed", "git refused. What it said is below.", {
      git: ["error: Your local changes would be overwritten", "Aborting"],
      git_more: 3,
    }),
  });
  renderApp(`/?folder=${REPO}`);
  const local = within(await (await changes()).findByRole("list", { name: m.localBranches }));
  await userEvent.click(local.getByRole("button", { name: m.switchTo("topic") }));
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("git refused.");
  expect(within(alert).getByLabelText(m.gitSaid)).toHaveTextContent(/would be overwritten\s+Aborting/);
  expect(alert).toHaveTextContent(m.andMoreLines(3));
});

test("the status is polled while the folder is selected, and no longer once another is", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    const fake = repoApi();
    const app = renderApp(`/?folder=${REPO}`, { pollMs: 1000 });
    await changes();
    const polled = () => fake.made("GET", "/api/git/status").length;
    const first = polled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3500);
    });
    expect(polled()).toBeGreaterThanOrEqual(first + 3); // positive control
    await app.navigate("/?folder=projects/plain");
    await screen.findByRole("region", { name: "projects/plain" });
    const after = polled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(polled()).toBe(after);
  } finally {
    vi.useRealTimers();
  }
});

test("an answer that arrives after a newer one is not shown", async () => {
  let answer: (value: unknown) => void = () => {};
  let calls = 0;
  repoApi({
    "GET /api/git/status": () => {
      calls += 1;
      if (calls === 1) return new Promise((resolve) => (answer = resolve));
      return status([], [{ path: "newer.txt", change: "untracked" }]);
    },
  });
  renderApp(`/?folder=${REPO}`);
  await userEvent.click(await (await changes()).findByRole("button", { name: m.refreshStatus }));
  expect(await (await changes()).findByText("newer.txt", { exact: false })).toBeInTheDocument();
  answer(status([], [{ path: "older.txt", change: "untracked" }]));
  await new Promise((resolve) => setTimeout(resolve, 50));
  expect((await changes()).queryByText("older.txt", { exact: false })).not.toBeInTheDocument();
});

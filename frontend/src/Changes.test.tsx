import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

import { m } from "./messages";
import { CONNECTED, fakeApi, ME, problem, type Routes } from "./test/fakeApi";
import { renderApp } from "./test/render";

const REPO = "projects/repo";
const entry = (path: string, repository = false) => ({
  name: path.split("/").pop(),
  path,
  has_children: false,
  repository,
});
const FOLDERS: Record<string, unknown> = {
  "": { path: "", entries: [{ ...entry("projects"), has_children: true }] },
  projects: { path: "projects", entries: [entry(REPO, true), entry("projects/plain")] },
};

type Files = {
  path: string;
  change: string;
  from?: string;
  submodule?: boolean;
  selectable?: boolean;
}[];

function status(staged: Files = [], notStaged: Files = [], extra: Record<string, unknown> = {}) {
  return {
    path: REPO,
    branch: {
      name: "main",
      commit: "0123456789abcdef",
      upstream: "origin/main",
      ahead: 1,
      behind: 2,
    },
    merging: false,
    staged: { files: staged, more: 0 },
    not_staged: { files: notStaged, more: 0 },
    author: { name: "Alice", email: "alice@example.com" },
    push_has_remote: true,
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
      { name: "origin/main", local: "main", taken_by: "main" },
      { name: "origin/feature", local: null, taken_by: null },
    ],
    more: 0,
  },
};

function repoApi(routes: Routes = {}) {
  return fakeApi({
    "GET /api/me": ME,
    "GET /api/claude": CONNECTED,
    "GET /api/servers": { servers: [] },
    "GET /api/folders": (c) =>
      FOLDERS[c.query.get("path") ?? ""] ?? problem(404, "path_not_found", "No."),
    "GET /api/git/status": status([], [{ path: "new.txt", change: "untracked" }]),
    "GET /api/git/branches": BRANCHES,
    ...routes,
  });
}

const changes = async () => within(await screen.findByRole("region", { name: m.changesTitle }));
const list = async (title: string) =>
  within(await (await changes()).findByRole("group", { name: title }));

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
      status(
        staged,
        [
          { path: "a.txt", change: "untracked" },
          { path: "b.txt", change: "modified" },
          { path: "c.txt", change: "deleted" },
        ].filter((f) => !staged.some((s) => s.path === f.path)),
      ),
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
  expect(fake.made("POST", "/api/git/stage")[0].body).toEqual({
    path: REPO,
    files: ["a.txt", "c.txt"],
  });
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
  for (const name of [/a\.txt/, /b\.txt/])
    expect(notStaged.getByRole("checkbox", { name })).toBeChecked();
  expect(all).toBeChecked();
  await userEvent.click(all);
  for (const name of [/a\.txt/, /b\.txt/])
    expect(notStaged.getByRole("checkbox", { name })).not.toBeChecked();
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
  files = [
    { path: "b.txt", change: "untracked" },
    { path: "c.txt", change: "untracked" },
  ];
  // a refresh: an action's, here a fetch of the branches does not count, so use Refresh's status
  await userEvent.click((await changes()).getByRole("button", { name: m.refreshStatus }));
  expect(await notStaged.findByRole("checkbox", { name: /c\.txt/ })).not.toBeChecked();
  expect(notStaged.getByRole("checkbox", { name: /b\.txt/ })).toBeChecked();
  await userEvent.click(notStaged.getByRole("button", { name: m.stageSelected }));
  await waitFor(() => expect(fake.made("POST", "/api/git/stage")).toHaveLength(1));
  expect(fake.made("POST", "/api/git/stage")[0].body).toEqual({ path: REPO, files: ["b.txt"] });
});

test("without a commit author, Commit is not offered and the section says where to set one", async () => {
  repoApi({
    "GET /api/git/status": status([{ path: "a.txt", change: "added" }], [], { author: null }),
  });
  const app = renderApp(`/?folder=${REPO}`);
  const section = await changes();
  expect(
    await section.findByText(m.commitNeedsAuthor.before.trim(), { exact: false }),
  ).toBeInTheDocument();
  expect(section.queryByRole("button", { name: m.commit })).not.toBeInTheDocument();
  expect(section.queryByLabelText(m.commitMessage)).not.toBeInTheDocument();
  await userEvent.click(section.getByRole("link", { name: m.commitNeedsAuthor.link }));
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
  expect(fake.made("POST", "/api/git/commit")[0].body).toEqual({
    path: REPO,
    message: "Add a.txt\nWith a body",
  });
  await waitFor(() => expect(box).toHaveValue(""));
});

test("an empty message is sent, and the server's refusal is shown by the box's label", async () => {
  const fake = repoApi({
    "GET /api/git/status": status([{ path: "a.txt", change: "added" }]),
    "POST /api/git/commit": problem(
      422,
      "validation_failed",
      "Some of what was sent is not valid.",
      {
        errors: [{ field: "message", message: "planted reason for the message" }],
      },
    ),
  });
  renderApp(`/?folder=${REPO}`);
  await userEvent.click(await (await changes()).findByRole("button", { name: m.commit }));
  await waitFor(() => expect(fake.made("POST", "/api/git/commit")).toHaveLength(1));
  expect(fake.made("POST", "/api/git/commit")[0].body).toEqual({ path: REPO, message: "" });
  expect(
    await screen.findByText(`${m.commitMessage}: planted reason for the message`),
  ).toBeInTheDocument();
});

test("the branch is shown with its upstream, or detached, or before the first commit", async () => {
  repoApi();
  renderApp(`/?folder=${REPO}`);
  expect(await (await changes()).findByText(m.onBranch("main"))).toBeInTheDocument();
  expect((await changes()).getByText(m.tracking("origin/main", 1, 2))).toBeInTheDocument();
});

test.each([
  [
    { name: null, commit: "0123456789abcdef", upstream: null, ahead: null, behind: null },
    () => m.detachedAt("0123456"),
  ],
  [
    { name: "main", commit: null, upstream: null, ahead: null, behind: null },
    () => m.noCommitsYet("main"),
  ],
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
  expect(fake.made("POST", "/api/git/switch")[0].body).toEqual({
    path: REPO,
    branch: "topic",
    remote: false,
  });
  await userEvent.click(remote.getByRole("button", { name: m.switchTo("origin/feature") }));
  await waitFor(() => expect(fake.made("POST", "/api/git/switch")).toHaveLength(2));
  expect(fake.made("POST", "/api/git/switch")[1].body).toEqual({
    path: REPO,
    branch: "origin/feature",
    remote: true,
  });
});

test("New branch sends the name, and Refresh branches fetches and lists them again", async () => {
  const fake = repoApi({
    "POST /api/git/branch": { path: REPO, branch: "topic/new" },
    "POST /api/git/fetch": { path: REPO },
  });
  renderApp(`/?folder=${REPO}`);
  const section = await changes();
  await userEvent.click(await section.findByRole("button", { name: m.newBranch }));
  await userEvent.type(section.getByLabelText(m.newBranchName), "topic/new");
  await userEvent.click(section.getByRole("button", { name: m.createBranch }));
  await waitFor(() => expect(fake.made("POST", "/api/git/branch")).toHaveLength(1));
  expect(fake.made("POST", "/api/git/branch")[0].body).toEqual({ path: REPO, name: "topic/new" });
  await waitFor(() => expect(section.queryByLabelText(m.newBranchName)).not.toBeInTheDocument());
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
  expect(within(alert).getByLabelText(m.gitSaid)).toHaveTextContent(
    /would be overwritten\s+Aborting/,
  );
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

test("Select all leaves out a file that cannot be selected, and Stage sends only the others", async () => {
  const fake = repoApi({
    "GET /api/git/status": status(
      [],
      [
        { path: "a.txt", change: "untracked" },
        { path: "caf�.txt", change: "untracked", selectable: false },
      ],
    ),
    "POST /api/git/stage": { path: REPO },
  });
  renderApp(`/?folder=${REPO}`);
  const notStaged = await list(m.notStagedTitle);
  await userEvent.click(notStaged.getByRole("checkbox", { name: m.selectAll }));
  await userEvent.click(notStaged.getByRole("button", { name: m.stageSelected }));
  await waitFor(() => expect(fake.made("POST", "/api/git/stage")).toHaveLength(1));
  expect(fake.made("POST", "/api/git/stage")[0].body).toEqual({ path: REPO, files: ["a.txt"] });
});

test("each list's heading says how many files it holds, those not shown too", async () => {
  repoApi({
    "GET /api/git/status": {
      ...status([{ path: "a.txt", change: "added" }]),
      not_staged: {
        files: [
          { path: "b.txt", change: "untracked" },
          { path: "c.txt", change: "untracked" },
        ],
        more: 5,
      },
    },
  });
  renderApp(`/?folder=${REPO}`);
  expect((await list(m.stagedTitle)).getByRole("heading")).toHaveTextContent(
    m.listHeading(m.stagedTitle, 1),
  );
  expect((await list(m.notStagedTitle)).getByRole("heading")).toHaveTextContent(
    m.listHeading(m.notStagedTitle, 7),
  );
});

test("the branch lists have headings, and a remote branch says which local branch tracks it", async () => {
  repoApi();
  renderApp(`/?folder=${REPO}`);
  const section = await changes();
  expect(await section.findByRole("heading", { name: m.localBranches })).toBeVisible();
  expect(section.getByRole("heading", { name: m.remoteBranches })).toBeVisible();
  const remote = within(section.getByRole("list", { name: m.remoteBranches }));
  expect(remote.getByText(m.trackedBy("main"))).toBeInTheDocument();
  const local = within(section.getByRole("list", { name: m.localBranches }));
  expect(local.getByText(m.tracks("origin/main"))).toBeInTheDocument();
  expect(local.getByText(m.currentBranch)).toBeInTheDocument();
});

test("a remote branch whose name a local branch has is not offered for switching, and says which", async () => {
  repoApi({
    "GET /api/git/branches": {
      ...BRANCHES,
      remote: {
        branches: [
          { name: "origin/main", local: "main", taken_by: "main" },
          { name: "origin/feature", local: null, taken_by: "feature" },
          { name: "origin/free", local: null, taken_by: null },
        ],
        more: 0,
      },
    },
  });
  renderApp(`/?folder=${REPO}`);
  const remote = within(await (await changes()).findByRole("list", { name: m.remoteBranches }));
  expect(remote.getByRole("button", { name: m.switchTo("origin/free") })).toBeInTheDocument(); // positive control
  const taken = remote.getByText("origin/feature").closest("li")!;
  expect(within(taken).queryByRole("button")).not.toBeInTheDocument();
  expect(taken).toHaveTextContent(m.nameTakenBy("feature"));
  const tracked = remote.getByText("origin/main").closest("li")!;
  expect(tracked).toHaveTextContent(m.trackedBy("main"));
  expect(tracked).not.toHaveTextContent(m.nameTakenBy("main"));
});

test("New branch is opened by a secondary button, and Commit is the section's one primary", async () => {
  repoApi({ "GET /api/git/status": status([{ path: "a.txt", change: "added" }]) });
  renderApp(`/?folder=${REPO}`);
  const section = await changes();
  await section.findByRole("button", { name: m.commit });
  expect(section.queryByLabelText(m.newBranchName)).not.toBeInTheDocument();
  const opener = section.getByRole("button", { name: m.newBranch });
  expect(opener).toHaveClass("secondary");
  const primaries = () => [
    ...document.querySelectorAll(`[aria-label="${m.changesTitle}"] button.primary`),
  ];
  expect(primaries().map((b) => b.textContent)).toEqual([m.commit]);
  await userEvent.click(opener);
  expect(section.getByLabelText(m.newBranchName)).toBeInTheDocument();
  await userEvent.click(section.getByRole("button", { name: m.cancel }));
  expect(section.queryByLabelText(m.newBranchName)).not.toBeInTheDocument();
});

test("without an author, the section says so once, with the link to Settings in it", async () => {
  repoApi({
    "GET /api/git/status": status([{ path: "a.txt", change: "added" }], [], { author: null }),
  });
  renderApp(`/?folder=${REPO}`);
  const link = await (await changes()).findByRole("link", { name: m.commitNeedsAuthor.link });
  expect(link.closest("p")).toHaveTextContent(m.commitNeedsAuthor.before.trim());
  expect(link.closest("p")!.textContent!.match(/author/gi)).toHaveLength(1); // said once
});

test("a refusal goes once a later action in the section works", async () => {
  repoApi({
    "POST /api/git/switch": problem(409, "git_failed", "git refused. What it said is below.", {
      git: ["no"],
      git_more: 0,
    }),
    "POST /api/git/stage": { path: REPO },
  });
  renderApp(`/?folder=${REPO}`);
  const section = await changes();
  const local = within(await section.findByRole("list", { name: m.localBranches }));
  await userEvent.click(local.getByRole("button", { name: m.switchTo("topic") }));
  expect(await section.findByRole("alert")).toHaveTextContent("git refused.");
  const notStaged = await list(m.notStagedTitle);
  await userEvent.click(notStaged.getByRole("checkbox", { name: /new\.txt/ }));
  await userEvent.click(notStaged.getByRole("button", { name: m.stageSelected }));
  await waitFor(() => expect(section.queryByRole("alert")).not.toBeInTheDocument());
});

test("a merge under way is said", async () => {
  repoApi({ "GET /api/git/status": status([], [], { merging: true }) });
  renderApp(`/?folder=${REPO}`);
  expect(await (await changes()).findByText(m.mergeInProgress)).toBeInTheDocument();
});

test("no merge, nothing said of one", async () => {
  repoApi({ "GET /api/git/status": status([], [], { merging: false }) });
  renderApp(`/?folder=${REPO}`);
  await (await changes()).findByText(m.onBranch("main")); // positive control: the status is shown
  expect((await changes()).queryByText(m.mergeInProgress)).not.toBeInTheDocument();
});

test("Cancel on New branch also takes away its refusal", async () => {
  repoApi({
    "POST /api/git/branch": problem(409, "git_failed", "git refused. What it said is below.", {
      git: ["exists"],
      git_more: 0,
    }),
  });
  renderApp(`/?folder=${REPO}`);
  const section = await changes();
  await userEvent.click(await section.findByRole("button", { name: m.newBranch }));
  await userEvent.type(section.getByLabelText(m.newBranchName), "main");
  await userEvent.click(section.getByRole("button", { name: m.createBranch }));
  expect(await section.findByRole("alert")).toHaveTextContent("git refused."); // positive control
  await userEvent.click(section.getByRole("button", { name: m.cancel }));
  expect(section.queryByRole("alert")).not.toBeInTheDocument();
});

test("the no-author sentence is one message, its link a part of it", async () => {
  repoApi({
    "GET /api/git/status": status([{ path: "a.txt", change: "added" }], [], { author: null }),
  });
  renderApp(`/?folder=${REPO}`);
  const link = await (await changes()).findByRole("link", { name: m.commitNeedsAuthor.link });
  const { before, link: linked, after } = m.commitNeedsAuthor;
  expect(link.closest("p")!.textContent).toBe(`${before}${linked}${after}`);
});

// --- push ----------------------------------------------------------------------

const STAGED: Files = [{ path: "a.txt", change: "added" }];
const PUSHED = { path: REPO, branch: "main", upstream: "origin/main", upstream_set: false };
// What git-status says of a push: where it goes, and for GitHub whether a token is stored.
const ELSEWHERE = { push_to_github: false, github_token: null };
const NO_TOKEN = { push_to_github: true, github_token: false };
const TOKEN = { push_to_github: true, github_token: true };

test("Push is offered on a branch whose pushes go elsewhere than GitHub, with no token", async () => {
  const fake = repoApi({
    "GET /api/git/status": status(STAGED, [], ELSEWHERE),
    "POST /api/git/push": PUSHED,
  });
  renderApp(`/?folder=${REPO}`);
  const section = await changes();
  const push = await section.findByRole("button", { name: m.push });
  expect(push).toHaveClass("secondary");
  expect(section.getByRole("button", { name: m.commitPush })).toBeInTheDocument();
  await userEvent.click(push);
  await waitFor(() => expect(fake.made("POST", "/api/git/push")).toHaveLength(1));
  expect(fake.made("POST", "/api/git/push")[0].body).toEqual({ path: REPO });
  expect(await section.findByText(m.pushed("main", "origin/main"))).toBeInTheDocument();
});

test("a push to GitHub is offered only with a token; otherwise the section says how to add one", async () => {
  repoApi({ "GET /api/git/status": status(STAGED, [], NO_TOKEN) });
  const app = renderApp(`/?folder=${REPO}`);
  const section = await changes();
  expect(
    await section.findByText(m.pushNeedsToken.before.trim(), { exact: false }),
  ).toBeInTheDocument();
  expect(section.queryByRole("button", { name: m.push })).not.toBeInTheDocument();
  expect(section.queryByRole("button", { name: m.commitPush })).not.toBeInTheDocument();
  expect(section.getByRole("button", { name: m.commit })).toBeInTheDocument(); // positive control
  await userEvent.click(section.getByRole("link", { name: m.pushNeedsToken.link }));
  expect(app.location().pathname).toBe("/settings");
});

test("with a token stored, a push to GitHub is offered", async () => {
  repoApi({ "GET /api/git/status": status(STAGED, [], TOKEN) });
  renderApp(`/?folder=${REPO}`);
  const section = await changes();
  expect(await section.findByRole("button", { name: m.push })).toBeInTheDocument();
  expect(section.getByRole("button", { name: m.commitPush })).toBeInTheDocument();
  expect(
    section.queryByText(m.pushNeedsToken.before.trim(), { exact: false }),
  ).not.toBeInTheDocument();
});

test("whether a token is stored comes with the status, so a token GitHub refused stops Push at the next refresh", async () => {
  let stored = true;
  const fake = repoApi({
    "GET /api/git/status": () => status(STAGED, [], { push_to_github: true, github_token: stored }),
  });
  renderApp(`/?folder=${REPO}`, { pollMs: 50 });
  const section = await changes();
  expect(await section.findByRole("button", { name: m.push })).toBeInTheDocument();
  stored = false; // git forgot the token after GitHub refused it
  expect(
    await section.findByText(m.pushNeedsToken.before.trim(), { exact: false }),
  ).toBeInTheDocument();
  expect(section.queryByRole("button", { name: m.push })).not.toBeInTheDocument();
  expect(fake.made("GET", "/api/github")).toHaveLength(0);
});

test("a status that does not say a token is stored offers no push to GitHub", async () => {
  repoApi({
    "GET /api/git/status": status(STAGED, [], { push_to_github: true, github_token: null }),
  });
  renderApp(`/?folder=${REPO}`);
  const section = await changes();
  expect(
    await section.findByText(m.pushNeedsToken.before.trim(), { exact: false }),
  ).toBeInTheDocument();
  expect(section.queryByRole("button", { name: m.push })).not.toBeInTheDocument();
  expect(section.queryByRole("button", { name: m.commitPush })).not.toBeInTheDocument();
});

test("until the status is known, neither Push nor why it is not offered is shown", async () => {
  let answer: (value: unknown) => void = () => {};
  repoApi({ "GET /api/git/status": () => new Promise((resolve) => (answer = resolve)) });
  renderApp(`/?folder=${REPO}`);
  const section = await changes();
  expect(await section.findByText(m.loading)).toBeInTheDocument();
  expect(
    section.queryByText(m.pushNeedsToken.before.trim(), { exact: false }),
  ).not.toBeInTheDocument();
  expect(section.queryByText(m.pushNeedsBranch)).not.toBeInTheDocument();
  expect(section.queryByRole("button", { name: m.push })).not.toBeInTheDocument();
  expect(section.queryByRole("button", { name: m.commitPush })).not.toBeInTheDocument();
  await act(async () => answer(status(STAGED, [], TOKEN)));
  expect(await section.findByRole("button", { name: m.push })).toBeInTheDocument(); // positive control
});

test("while no branch is checked out, nothing is pushed and the section says why", async () => {
  const detached = {
    name: null,
    commit: "0123456789abcdef",
    upstream: null,
    ahead: null,
    behind: null,
  };
  repoApi({ "GET /api/git/status": status(STAGED, [], { branch: detached, ...TOKEN }) });
  renderApp(`/?folder=${REPO}`);
  const section = await changes();
  expect(await section.findByText(m.pushNeedsBranch)).toBeInTheDocument();
  expect(section.queryByRole("button", { name: m.push })).not.toBeInTheDocument();
  expect(section.queryByRole("button", { name: m.commitPush })).not.toBeInTheDocument();
  expect(section.getByRole("button", { name: m.commit })).toBeInTheDocument(); // positive control
});

test("Push is not offered for a branch where it pushes has already, and Commit & Push still is", async () => {
  const even = {
    name: "main",
    commit: "0123456789abcdef",
    upstream: "origin/main",
    ahead: 3,
    behind: 2,
  };
  repoApi({ "GET /api/git/status": status(STAGED, [], { branch: even, push_ahead: 0, ...TOKEN }) });
  renderApp(`/?folder=${REPO}`);
  const section = await changes();
  expect(await section.findByRole("button", { name: m.commitPush })).toBeInTheDocument();
  expect(section.queryByRole("button", { name: m.push })).not.toBeInTheDocument();
});

test("Push is offered for a branch with no upstream yet", async () => {
  const fresh = {
    name: "topic",
    commit: "0123456789abcdef",
    upstream: null,
    ahead: null,
    behind: null,
  };
  repoApi({ "GET /api/git/status": status([], [], { branch: fresh, ...TOKEN }) });
  renderApp(`/?folder=${REPO}`);
  expect(await (await changes()).findByRole("button", { name: m.push })).toBeInTheDocument();
});

test("Commit & Push is offered only where Commit is", async () => {
  repoApi({ "GET /api/git/status": status([], [], ELSEWHERE) });
  renderApp(`/?folder=${REPO}`);
  const section = await changes();
  expect(await section.findByRole("button", { name: m.push })).toBeInTheDocument(); // positive control
  expect(section.getByText(m.commitNothingStaged)).toBeInTheDocument();
  expect(section.queryByRole("button", { name: m.commitPush })).not.toBeInTheDocument();
});

test("Commit & Push sends the message, says what was pushed, and empties the box", async () => {
  const fake = repoApi({
    "GET /api/git/status": status(STAGED, [], ELSEWHERE),
    "POST /api/git/commit-push": {
      path: REPO,
      commit: "abcdef1234",
      push: PUSHED,
      push_problem: null,
    },
  });
  renderApp(`/?folder=${REPO}`);
  const section = await changes();
  const box = await section.findByLabelText(m.commitMessage);
  await userEvent.type(box, "Add a.txt");
  await userEvent.click(section.getByRole("button", { name: m.commitPush }));
  await waitFor(() => expect(fake.made("POST", "/api/git/commit-push")).toHaveLength(1));
  expect(fake.made("POST", "/api/git/commit-push")[0].body).toEqual({
    path: REPO,
    message: "Add a.txt",
  });
  expect(fake.made("POST", "/api/git/commit")).toHaveLength(0);
  expect(await section.findByText(m.pushed("main", "origin/main"))).toBeInTheDocument();
  await waitFor(() => expect(box).toHaveValue(""));
});

test("a commit made whose push failed says both, with what git said", async () => {
  repoApi({
    "GET /api/git/status": status(STAGED, [], ELSEWHERE),
    "POST /api/git/commit-push": {
      path: REPO,
      commit: "abcdef1234",
      push: null,
      push_problem: {
        type: "urn:rcman:problem:git_failed",
        title: "planted title of the push's problem",
        status: 409,
        code: "git_failed",
        git: ["planted words of git"],
        git_more: 0,
      },
    },
  });
  renderApp(`/?folder=${REPO}`);
  const section = await changes();
  const box = await section.findByLabelText(m.commitMessage);
  await userEvent.type(box, "Add a.txt");
  await userEvent.click(section.getByRole("button", { name: m.commitPush }));
  expect(await section.findByText(m.committedNotPushed("abcdef1"))).toBeInTheDocument();
  const problem = section.getByRole("alert");
  expect(problem).toHaveTextContent("planted title of the push's problem");
  expect(within(problem).getByLabelText(m.gitSaid)).toHaveTextContent("planted words of git");
  await waitFor(() => expect(box).toHaveValue(""));
  expect(section.queryByText(m.pushed("main", "origin/main"))).not.toBeInTheDocument();
});

test("without a token for a push to GitHub, the section says so in one sentence, the link to Settings in it", async () => {
  repoApi({ "GET /api/git/status": status(STAGED, [], NO_TOKEN) });
  renderApp(`/?folder=${REPO}`);
  const link = await (await changes()).findByRole("link", { name: m.pushNeedsToken.link });
  const { before, link: linked, after } = m.pushNeedsToken;
  expect(link.closest("p")!.textContent).toBe(`${before}${linked}${after}`);
});

test("Push is offered for a branch ahead of where it pushes, even when its upstream has it all", async () => {
  const even = {
    name: "main",
    commit: "0123456789abcdef",
    upstream: "origin/main",
    ahead: 0,
    behind: 0,
  };
  repoApi({ "GET /api/git/status": status([], [], { branch: even, push_ahead: 1, ...TOKEN }) });
  renderApp(`/?folder=${REPO}`);
  expect(await (await changes()).findByRole("button", { name: m.push })).toBeInTheDocument();
});

test("with nowhere to push, neither Push nor Commit & Push is offered, and the section says why", async () => {
  const fresh = {
    name: "topic",
    commit: "0123456789abcdef",
    upstream: null,
    ahead: null,
    behind: null,
  };
  repoApi({
    "GET /api/git/status": status(STAGED, [], {
      branch: fresh,
      ...ELSEWHERE,
      push_has_remote: false,
    }),
  });
  renderApp(`/?folder=${REPO}`);
  const section = await changes();
  expect(await section.findByRole("button", { name: m.commit })).toBeInTheDocument(); // positive control
  expect(section.queryByRole("button", { name: m.push })).not.toBeInTheDocument();
  expect(section.queryByRole("button", { name: m.commitPush })).not.toBeInTheDocument();
  expect(section.getByText(m.pushNeedsRemote)).toBeInTheDocument();
});

test("a status that does not say a push has a remote offers no push", async () => {
  repoApi({
    "GET /api/git/status": status(STAGED, [], { ...ELSEWHERE, push_has_remote: undefined }),
  });
  renderApp(`/?folder=${REPO}`);
  const section = await changes();
  expect(await section.findByRole("button", { name: m.commit })).toBeInTheDocument(); // positive control
  expect(section.queryByRole("button", { name: m.push })).not.toBeInTheDocument();
  expect(section.queryByRole("button", { name: m.commitPush })).not.toBeInTheDocument();
  expect(section.getByText(m.pushNeedsRemote)).toBeInTheDocument();
});

test("on a branch with no commit yet, Push is not offered and the section says why; Commit & Push is", async () => {
  const unborn = { name: "trunk", commit: null, upstream: null, ahead: null, behind: null };
  repoApi({
    "GET /api/git/status": status(STAGED, [], { branch: unborn, ...ELSEWHERE, push_ahead: null }),
  });
  renderApp(`/?folder=${REPO}`);
  const section = await changes();
  expect(await section.findByRole("button", { name: m.commitPush })).toBeInTheDocument();
  expect(section.queryByRole("button", { name: m.push })).not.toBeInTheDocument();
  expect(section.getByText(m.pushNeedsCommit)).toBeInTheDocument();
});

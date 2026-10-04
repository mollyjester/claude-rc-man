import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useLayoutEffect } from "react";
import { vi } from "vitest";

import { CHECKED_TICK_MS, Files as FileList } from "./Changes";
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
// The branch lookup's button, named for where the repository is.
const lookup = async (name = m.onBranch("main")) =>
  (await changes()).findByRole("button", { name });
// What a disclosure button opens.
const panelOf = (button: HTMLElement) =>
  document.getElementById(button.getAttribute("aria-controls") ?? "");
async function openLookup(name?: string) {
  const button = await lookup(name);
  await userEvent.click(button);
  expect(button).toHaveAttribute("aria-expanded", "true");
  return within(panelOf(button)!);
}
// The branch bar beside the section's heading, once the status is known.
const bar = async () =>
  within(
    (await (await changes()).findByRole("button", { name: m.newBranch })).closest<HTMLElement>(
      ".branch-bar",
    )!,
  );
// Commit & Push, from the Commit button's menu.
async function commitAndPush() {
  const section = await changes();
  await userEvent.click(await section.findByRole("button", { name: m.commitMenu }));
  await userEvent.click(section.getByRole("button", { name: m.commitPush }));
}

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
  expect(staged.getByTitle(m.changes.renamed)).toHaveTextContent(m.changeLetters.renamed);
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

test("the branch bar names the branch, and shows how far it is from its upstream", async () => {
  repoApi();
  renderApp(`/?folder=${REPO}`);
  const button = await lookup();
  expect(button).toHaveTextContent("main");
  expect(button).toHaveAttribute("aria-expanded", "false");
  const sync = (await bar()).getByTitle(m.tracking("origin/main", 1, 2));
  expect(sync).toHaveTextContent(m.tracking("origin/main", 1, 2)); // as its accessible text
  const arrows = sync.querySelector(".sync-arrows")!;
  expect(arrows).toHaveAttribute("aria-hidden", "true");
  expect(arrows.querySelectorAll("svg")).toHaveLength(2);
  expect(arrows).toHaveTextContent(/^1\s*2$/);
});

test("a branch level with its upstream shows 0 and 0", async () => {
  const level = {
    name: "main",
    commit: "0123456789abcdef",
    upstream: "origin/main",
    ahead: 0,
    behind: 0,
  };
  repoApi({ "GET /api/git/status": status([], [], { branch: level }) });
  renderApp(`/?folder=${REPO}`);
  const sync = (await bar()).getByTitle(m.tracking("origin/main", 0, 0));
  expect(sync.querySelector(".sync-arrows")).toHaveTextContent(/^0\s*0$/);
});

test("a branch whose upstream is gone says so, with no arrows", async () => {
  const gone = {
    name: "main",
    commit: "0123456789abcdef",
    upstream: "origin/main",
    ahead: null,
    behind: null,
  };
  repoApi({ "GET /api/git/status": status([], [], { branch: gone }) });
  renderApp(`/?folder=${REPO}`);
  const here = await bar();
  expect(here.getByText(m.tracking("origin/main", null, null))).toBeVisible();
  expect(here.getByRole("button", { name: m.onBranch("main") })).toBeInTheDocument(); // positive control
  expect(document.querySelector(".branch-bar .sync-arrows")).toBeNull();
  expect(document.querySelectorAll(".branch-bar svg").length).toBeGreaterThan(0); // the bar's own icons are there
});

test("a branch with no upstream shows nothing of one", async () => {
  const fresh = {
    name: "topic",
    commit: "0123456789abcdef",
    upstream: null,
    ahead: null,
    behind: null,
  };
  repoApi({ "GET /api/git/status": status([], [], { branch: fresh }) });
  renderApp(`/?folder=${REPO}`);
  const button = await lookup(m.onBranch("topic"));
  expect(button).toHaveTextContent("topic"); // positive control: the bar is there
  const shown = button.closest(".branch-bar")!;
  expect(shown.querySelector(".sync")).toBeNull();
  expect(shown).not.toHaveTextContent(/Tracking/);
});

test.each([
  [
    { name: null, commit: "0123456789abcdef", upstream: null, ahead: null, behind: null },
    () => m.detachedAt("0123456"),
    "0123456",
  ],
  [
    { name: "main", commit: null, upstream: null, ahead: null, behind: null },
    () => m.noCommitsYet("main"),
    "main",
  ],
])("a branch that is %o is named as such by the lookup", async (branch, name, shown) => {
  repoApi({ "GET /api/git/status": status([], [], { branch }) });
  renderApp(`/?folder=${REPO}`);
  expect(await lookup(name())).toHaveTextContent(shown);
});

test("the lookup is closed until opened, and lists the local and the remote branches", async () => {
  const fake = repoApi();
  renderApp(`/?folder=${REPO}`);
  const button = await lookup();
  await waitFor(() => expect(fake.made("GET", "/api/git/branches").length).toBeGreaterThan(0));
  expect(button).toHaveAttribute("aria-expanded", "false");
  expect(panelOf(button)).toBeNull();
  expect(screen.queryByRole("button", { name: m.switchTo("topic") })).not.toBeInTheDocument();
  const popup = await openLookup();
  const local = within(popup.getByRole("list", { name: m.localBranches }));
  const remote = within(popup.getByRole("list", { name: m.remoteBranches }));
  expect(local.getByRole("button", { name: m.switchTo("topic") })).toBeInTheDocument();
  expect(remote.getByRole("button", { name: m.switchTo("origin/feature") })).toBeInTheDocument();
  expect(popup.getByText(m.localBranches)).toBeVisible();
  expect(popup.getByText(m.remoteBranches)).toBeVisible();
  // the pop-up is a disclosure, not a dialog or a listbox
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
});

test("the current branch is marked and not switchable; a remote branch says which local branch tracks it", async () => {
  repoApi();
  renderApp(`/?folder=${REPO}`);
  const popup = await openLookup();
  const local = within(popup.getByRole("list", { name: m.localBranches }));
  const remote = within(popup.getByRole("list", { name: m.remoteBranches }));
  expect(local.getByRole("button", { name: m.switchTo("topic") })).toBeInTheDocument(); // positive control
  const current = local.getByText("main", { exact: true }).closest("li")!;
  expect(within(current).queryByRole("button")).not.toBeInTheDocument();
  expect(current).toHaveTextContent(m.currentBranch);
  expect(current).toHaveTextContent(m.tracks("origin/main"));
  const tracked = remote.getByText("origin/main", { exact: true }).closest("li")!;
  expect(within(tracked).queryByRole("button")).not.toBeInTheDocument();
  expect(tracked).toHaveTextContent(m.trackedBy("main"));
});

test("Switch from the lookup sends the branch, whether it is remote, and closes the lookup", async () => {
  const fake = repoApi({ "POST /api/git/switch": { path: REPO, branch: "x" } });
  renderApp(`/?folder=${REPO}`);
  let popup = await openLookup();
  await userEvent.click(popup.getByRole("button", { name: m.switchTo("topic") }));
  await waitFor(() => expect(fake.made("POST", "/api/git/switch")).toHaveLength(1));
  expect(fake.made("POST", "/api/git/switch")[0].body).toEqual({
    path: REPO,
    branch: "topic",
    remote: false,
  });
  const button = await lookup();
  expect(button).toHaveAttribute("aria-expanded", "false");
  expect(button).toHaveFocus();
  popup = await openLookup();
  await userEvent.click(popup.getByRole("button", { name: m.switchTo("origin/feature") }));
  await waitFor(() => expect(fake.made("POST", "/api/git/switch")).toHaveLength(2));
  expect(fake.made("POST", "/api/git/switch")[1].body).toEqual({
    path: REPO,
    branch: "origin/feature",
    remote: true,
  });
  expect(await lookup()).toHaveAttribute("aria-expanded", "false");
});

test("the lookup's filter narrows both groups, whatever the case, and asks rcman nothing", async () => {
  const fake = repoApi();
  renderApp(`/?folder=${REPO}`);
  const popup = await openLookup();
  await waitFor(() => expect(popup.getAllByRole("listitem").length).toBe(4));
  const asked = fake.calls.length;
  await userEvent.type(popup.getByRole("textbox", { name: m.findBranch }), "FEAT");
  const local = within(popup.getByRole("list", { name: m.localBranches }));
  const remote = within(popup.getByRole("list", { name: m.remoteBranches }));
  expect(remote.getByRole("button", { name: m.switchTo("origin/feature") })).toBeInTheDocument();
  expect(remote.queryByText("origin/main", { exact: true })).not.toBeInTheDocument();
  expect(local.queryByText("topic", { exact: true })).not.toBeInTheDocument();
  expect(local.getByText(m.noBranchMatches)).toBeInTheDocument();
  expect(remote.queryByText(m.noBranchMatches)).not.toBeInTheDocument();
  await userEvent.clear(popup.getByRole("textbox", { name: m.findBranch }));
  expect(local.getByText("topic", { exact: true })).toBeInTheDocument();
  expect(fake.calls.length).toBe(asked);
  expect(popup.getByRole("textbox", { name: m.findBranch })).toHaveAttribute("type", "text");
});

test("each group of the lookup says how many more branches there are", async () => {
  repoApi({
    "GET /api/git/branches": {
      ...BRANCHES,
      local: { ...BRANCHES.local, more: 3 },
      remote: { ...BRANCHES.remote, more: 7 },
    },
  });
  renderApp(`/?folder=${REPO}`);
  const popup = await openLookup();
  expect(await popup.findByText(m.andMore(3))).toBeInTheDocument();
  expect(popup.getByText(m.andMore(7))).toBeInTheDocument();
});

test("a group with no branches at all says so", async () => {
  repoApi({ "GET /api/git/branches": { ...BRANCHES, remote: { branches: [], more: 0 } } });
  renderApp(`/?folder=${REPO}`);
  const popup = await openLookup();
  const remote = within(await popup.findByRole("list", { name: m.remoteBranches }));
  expect(remote.getByText(m.noBranches)).toBeInTheDocument();
  const local = within(popup.getByRole("list", { name: m.localBranches }));
  expect(local.queryByText(m.noBranches)).not.toBeInTheDocument(); // only where there are none
});

test("New branch is a + that opens a form; Create sends the name and closes it", async () => {
  const fake = repoApi({ "POST /api/git/branch": { path: REPO, branch: "topic/new" } });
  renderApp(`/?folder=${REPO}`);
  const section = await changes();
  const plus = await section.findByRole("button", { name: m.newBranch });
  expect(plus).toHaveAttribute("aria-expanded", "false");
  expect(plus.textContent).toBe(""); // an icon only
  expect(plus.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  await userEvent.click(plus);
  expect(plus).toHaveAttribute("aria-expanded", "true");
  await userEvent.type(section.getByLabelText(m.newBranchName), "topic/new");
  await userEvent.click(section.getByRole("button", { name: m.create }));
  await waitFor(() => expect(fake.made("POST", "/api/git/branch")).toHaveLength(1));
  expect(fake.made("POST", "/api/git/branch")[0].body).toEqual({ path: REPO, name: "topic/new" });
  await waitFor(() => expect(section.queryByLabelText(m.newBranchName)).not.toBeInTheDocument());
  expect(plus).toHaveAttribute("aria-expanded", "false");
});

test("a refused new branch keeps the form open, with the reason under the field's label", async () => {
  repoApi({
    "POST /api/git/branch": problem(422, "validation_failed", "Not valid.", {
      errors: [{ field: "name", message: "planted reason for the name" }],
    }),
  });
  renderApp(`/?folder=${REPO}`);
  const section = await changes();
  const plus = await section.findByRole("button", { name: m.newBranch });
  await userEvent.click(plus);
  await userEvent.type(section.getByLabelText(m.newBranchName), "bad name");
  await userEvent.click(section.getByRole("button", { name: m.create }));
  const reason = await within(panelOf(plus)!).findByText(
    `${m.newBranchName}: planted reason for the name`,
  );
  expect(reason).toBeInTheDocument();
  expect(plus).toHaveAttribute("aria-expanded", "true");
});

test.each([
  ["Cancel", async () => userEvent.click(screen.getByRole("button", { name: m.cancel }))],
  ["Escape", async () => userEvent.keyboard("{Escape}")],
  [
    "a press outside",
    async () => fireEvent.pointerDown(screen.getByRole("heading", { name: m.changesTitle })),
  ],
])("%s closes New branch and takes away its refusal", async (_, close) => {
  repoApi({
    "POST /api/git/branch": problem(409, "git_failed", "git refused. What it said is below.", {
      git: ["exists"],
      git_more: 0,
    }),
  });
  renderApp(`/?folder=${REPO}`);
  const section = await changes();
  const plus = await section.findByRole("button", { name: m.newBranch });
  await userEvent.click(plus);
  await userEvent.type(section.getByLabelText(m.newBranchName), "main");
  await userEvent.click(section.getByRole("button", { name: m.create }));
  expect(await section.findByRole("alert")).toHaveTextContent("git refused."); // positive control
  await close();
  expect(plus).toHaveAttribute("aria-expanded", "false");
  expect(section.queryByLabelText(m.newBranchName)).not.toBeInTheDocument();
  expect(section.queryByRole("alert")).not.toBeInTheDocument();
  await userEvent.click(plus);
  expect(section.getByLabelText(m.newBranchName)).toBeInTheDocument();
  expect(section.queryByRole("alert")).not.toBeInTheDocument();
});

test("opening one of the bar's pop-ups closes the other", async () => {
  repoApi();
  renderApp(`/?folder=${REPO}`);
  await openLookup();
  const button = await lookup();
  const plus = (await changes()).getByRole("button", { name: m.newBranch });
  await userEvent.click(plus);
  expect(plus).toHaveAttribute("aria-expanded", "true");
  expect(button).toHaveAttribute("aria-expanded", "false");
  await userEvent.click(button);
  expect(button).toHaveAttribute("aria-expanded", "true");
  expect(plus).toHaveAttribute("aria-expanded", "false");
});

test("Refresh branches fetches, lists them again, and waits while it runs", async () => {
  let answer: (value: unknown) => void = () => {};
  const fake = repoApi({
    "POST /api/git/fetch": () => new Promise((resolve) => (answer = resolve)),
  });
  renderApp(`/?folder=${REPO}`);
  const refresh = (await bar()).getByRole("button", { name: m.refreshBranches });
  expect(refresh.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  await waitFor(() => expect(fake.made("GET", "/api/git/branches").length).toBeGreaterThan(0));
  expect(refresh).toBeEnabled();
  expect(refresh).not.toHaveClass("spinning");
  const listed = fake.made("GET", "/api/git/branches").length;
  await userEvent.click(refresh);
  await waitFor(() => expect(fake.made("POST", "/api/git/fetch")).toHaveLength(1));
  expect(fake.made("POST", "/api/git/fetch")[0].body).toEqual({ path: REPO });
  expect(refresh).toBeDisabled();
  expect(refresh).toHaveClass("spinning");
  await act(async () => answer({ path: REPO }));
  await waitFor(() => expect(refresh).toBeEnabled());
  expect(refresh).not.toHaveClass("spinning");
  await waitFor(() => expect(fake.made("GET", "/api/git/branches").length).toBeGreaterThan(listed));
  const fetched = fake.calls.findIndex((c) => c.path === "/api/git/fetch");
  expect(fake.calls.slice(fetched).some((c) => c.path === "/api/git/branches")).toBe(true);
});

test("a refusal of the branch chosen is named by the switch that was tried", async () => {
  repoApi({
    "POST /api/git/switch": problem(422, "validation_failed", "Not valid.", {
      errors: [{ field: "branch", message: "planted reason for the branch" }],
    }),
  });
  renderApp(`/?folder=${REPO}`);
  const popup = await openLookup();
  await userEvent.click(popup.getByRole("button", { name: m.switchTo("topic") }));
  expect(
    await (await changes()).findByText(`${m.switchTo("topic")}: planted reason for the branch`),
  ).toBeInTheDocument();
});

test("git's refusal is shown with what it said", async () => {
  repoApi({
    "POST /api/git/switch": problem(409, "git_failed", "git refused. What it said is below.", {
      git: ["error: Your local changes would be overwritten", "Aborting"],
      git_more: 3,
    }),
  });
  renderApp(`/?folder=${REPO}`);
  const popup = await openLookup();
  await userEvent.click(popup.getByRole("button", { name: m.switchTo("topic") }));
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

test("while the Changes section polls, only the selected folder's status is fetched, and the folder tree is not fetched again", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    const OTHER = "projects/other";
    const fake = repoApi({
      "GET /api/folders": (c) =>
        ({
          ...FOLDERS,
          projects: {
            path: "projects",
            entries: [entry(REPO, true), entry(OTHER, true), entry("projects/plain")],
          },
        })[c.query.get("path") ?? ""] ?? problem(404, "path_not_found", "No."),
    });
    renderApp(`/?folder=${REPO}`, { pollMs: 1000 });
    await changes();
    const tree = within(screen.getByRole("region", { name: m.foldersTitle }));
    expect(await tree.findByRole("button", { name: "other" })).toBeInTheDocument(); // another repository is listed
    const statuses = () => fake.made("GET", "/api/git/status");
    const folders = () => fake.made("GET", "/api/folders").length;
    const first = statuses().length;
    const fetched = folders();
    expect(fetched).toBeGreaterThan(0);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3500);
    });
    expect(statuses().length).toBeGreaterThanOrEqual(first + 3); // positive control: it polls
    expect(folders()).toBe(fetched);
    expect(new Set(statuses().map((c) => c.query.get("path")))).toEqual(new Set([REPO]));
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

test("each list's heading is a disclosure that says how many files it holds, those not shown too", async () => {
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
  const staged = await list(m.stagedTitle);
  const toggle = staged.getByRole("button", { name: m.listHeading(m.stagedTitle, 1) });
  expect(staged.getByRole("heading")).toContainElement(toggle);
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  expect(panelOf(toggle)).toContainElement(staged.getByRole("checkbox", { name: /a\.txt/ }));
  expect(within(toggle).getByText("1")).toHaveClass("count");
  expect(
    (await list(m.notStagedTitle)).getByRole("button", {
      name: m.listHeading(m.notStagedTitle, 7),
    }),
  ).toHaveAttribute("aria-expanded", "true");
});

test("a list is open while it has files and closed while it has none, and its action is not shown while empty", async () => {
  repoApi();
  renderApp(`/?folder=${REPO}`);
  const notStaged = await list(m.notStagedTitle);
  const staged = await list(m.stagedTitle);
  const full = notStaged.getByRole("button", { name: m.listHeading(m.notStagedTitle, 1) });
  expect(full).toHaveAttribute("aria-expanded", "true");
  expect(notStaged.getByRole("button", { name: m.stageSelected })).toBeInTheDocument(); // positive control
  const empty = staged.getByRole("button", { name: m.listHeading(m.stagedTitle, 0) });
  expect(empty).toHaveAttribute("aria-expanded", "false");
  expect(panelOf(empty)).not.toBeVisible();
  expect(staged.queryByRole("button", { name: m.unstageSelected })).not.toBeInTheDocument();
  await userEvent.click(empty);
  expect(empty).toHaveAttribute("aria-expanded", "true");
  expect(staged.getByText(m.noFiles)).toBeVisible();
});

test("a list stays as it was left across refreshes, and opens when files come to it", async () => {
  let staged: Files = [];
  let notStagedFiles: Files = [{ path: "new.txt", change: "untracked" }];
  const fake = repoApi({ "GET /api/git/status": () => status(staged, notStagedFiles) });
  renderApp(`/?folder=${REPO}`);
  const section = await changes();
  const notStaged = (await list(m.notStagedTitle)).getByRole("button", {
    name: m.listHeading(m.notStagedTitle, 1),
  });
  await userEvent.click(notStaged);
  expect(notStaged).toHaveAttribute("aria-expanded", "false");
  expect((await list(m.notStagedTitle)).queryByRole("checkbox")).not.toBeInTheDocument();
  // a refresh that lands, seen by what it changes: a second file in the closed list
  const asked = fake.made("GET", "/api/git/status").length;
  notStagedFiles = [...notStagedFiles, { path: "other.txt", change: "untracked" }];
  await userEvent.click(section.getByRole("button", { name: m.refreshStatus }));
  await waitFor(() => expect(fake.made("GET", "/api/git/status").length).toBeGreaterThan(asked));
  await waitFor(() => expect(notStaged).toHaveAccessibleName(m.listHeading(m.notStagedTitle, 2)));
  expect(notStaged).toHaveAttribute("aria-expanded", "false");

  const stagedToggle = () =>
    section.getByRole("button", { name: (n) => n.startsWith(`${m.stagedTitle} (`) });
  expect(stagedToggle()).toHaveAttribute("aria-expanded", "false");
  staged = [{ path: "a.txt", change: "added" }];
  await userEvent.click(section.getByRole("button", { name: m.refreshStatus }));
  await waitFor(() => expect(stagedToggle()).toHaveAccessibleName(m.listHeading(m.stagedTitle, 1)));
  expect(stagedToggle()).toHaveAttribute("aria-expanded", "true");
  expect(notStaged).toHaveAttribute("aria-expanded", "false"); // the other as it was left
});

test("a list's action is beside its heading, not in it, and does not fold the list", async () => {
  const fake = repoApi({ "POST /api/git/stage": { path: REPO } });
  renderApp(`/?folder=${REPO}`);
  const notStaged = await list(m.notStagedTitle);
  const toggle = notStaged.getByRole("button", { name: m.listHeading(m.notStagedTitle, 1) });
  const action = notStaged.getByRole("button", { name: m.stageSelected });
  expect(toggle.closest("h4")).not.toContainElement(action);
  expect(toggle).not.toContainElement(action);
  await userEvent.click(notStaged.getByRole("checkbox", { name: /new\.txt/ }));
  await userEvent.click(action);
  await waitFor(() => expect(fake.made("POST", "/api/git/stage")).toHaveLength(1));
  expect(toggle).toHaveAttribute("aria-expanded", "true");
});

test("each change is shown as its letter, its word what it says and its title", async () => {
  const codes = Object.keys(m.changeLetters);
  repoApi({
    "GET /api/git/status": status(
      [],
      codes.map((change) => ({ path: `${change}.txt`, change })),
    ),
  });
  renderApp(`/?folder=${REPO}`);
  const notStaged = await list(m.notStagedTitle);
  expect(codes.length).toBeGreaterThan(0); // non-vacuity
  for (const code of codes) {
    const row = (await notStaged.findByText(`${code}.txt`)).closest("li")!;
    const mark = within(row).getByTitle(m.changes[code]);
    const letter = mark.querySelector('[aria-hidden="true"]')!;
    expect(letter).toHaveTextContent(m.changeLetters[code]);
    expect(letter).toHaveClass(`change-${code}`);
    expect(within(mark).getByText(m.changes[code])).toHaveClass("visually-hidden");
  }
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
  const popup = await openLookup();
  const remote = within(await popup.findByRole("list", { name: m.remoteBranches }));
  expect(remote.getByRole("button", { name: m.switchTo("origin/free") })).toBeInTheDocument(); // positive control
  const taken = remote.getByText("origin/feature").closest("li")!;
  expect(within(taken).queryByRole("button")).not.toBeInTheDocument();
  expect(taken).toHaveTextContent(m.nameTakenBy("feature"));
  // tracked by a local branch, which says so (held in the test above), not that its name is taken
  const tracked = remote.getByText("origin/main").closest("li")!;
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
  expect(primaries()).toHaveLength(2); // the form's own Create
  expect(primaries()[1]).toHaveAttribute("type", "submit");
  await userEvent.click(section.getByRole("button", { name: m.cancel }));
  expect(section.queryByLabelText(m.newBranchName)).not.toBeInTheDocument();
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
  const popup = await openLookup();
  await userEvent.click(popup.getByRole("button", { name: m.switchTo("topic") }));
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
  await lookup(); // positive control: the status is shown
  expect((await changes()).queryByText(m.mergeInProgress)).not.toBeInTheDocument();
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
  expect((await bar()).getByRole("button", { name: m.push })).toBe(push); // in the branch bar
  expect(section.getByRole("button", { name: m.commitMenu })).toBeInTheDocument();
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
  expect(section.queryByRole("button", { name: m.commitMenu })).not.toBeInTheDocument();
  expect(section.getByRole("button", { name: m.commit })).toBeInTheDocument(); // positive control
  await userEvent.click(section.getByRole("link", { name: m.pushNeedsToken.link }));
  expect(app.location().pathname).toBe("/settings");
});

test("with a token stored, a push to GitHub is offered", async () => {
  repoApi({ "GET /api/git/status": status(STAGED, [], TOKEN) });
  renderApp(`/?folder=${REPO}`);
  const section = await changes();
  expect(await section.findByRole("button", { name: m.push })).toBeInTheDocument();
  expect(section.getByRole("button", { name: m.commitMenu })).toBeInTheDocument();
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
  expect(section.queryByRole("button", { name: m.commitMenu })).not.toBeInTheDocument();
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
  expect(section.queryByRole("button", { name: m.commitMenu })).not.toBeInTheDocument();
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
  expect(section.queryByRole("button", { name: m.commitMenu })).not.toBeInTheDocument();
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
  expect(await section.findByRole("button", { name: m.commitMenu })).toBeInTheDocument();
  expect(section.queryByRole("button", { name: m.push })).not.toBeInTheDocument();
});

test("Push is offered, as Push, for a branch with no upstream yet", async () => {
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
  expect(section.queryByRole("button", { name: m.commitMenu })).not.toBeInTheDocument();
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
  await commitAndPush();
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
  await commitAndPush();
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
  expect((await bar()).getByRole("button", { name: m.push })).toBeInTheDocument();
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
  expect(section.queryByRole("button", { name: m.commitMenu })).not.toBeInTheDocument();
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
  expect(section.queryByRole("button", { name: m.commitMenu })).not.toBeInTheDocument();
  expect(section.getByText(m.pushNeedsRemote)).toBeInTheDocument();
});

test("on a branch with no commit yet, Push is not offered and the section says why; Commit & Push is", async () => {
  const unborn = { name: "trunk", commit: null, upstream: null, ahead: null, behind: null };
  repoApi({
    "GET /api/git/status": status(STAGED, [], { branch: unborn, ...ELSEWHERE, push_ahead: null }),
  });
  renderApp(`/?folder=${REPO}`);
  const section = await changes();
  expect(await section.findByRole("button", { name: m.commitMenu })).toBeInTheDocument();
  expect(section.queryByRole("button", { name: m.push })).not.toBeInTheDocument();
  expect(section.getByText(m.pushNeedsCommit)).toBeInTheDocument();
});

test("Commit's menu holds Commit & Push, which is not a primary button", async () => {
  repoApi({ "GET /api/git/status": status(STAGED, [], ELSEWHERE) });
  renderApp(`/?folder=${REPO}`);
  const section = await changes();
  const commit = await section.findByRole("button", { name: m.commit });
  expect(commit).toHaveAttribute("type", "submit");
  expect(commit).toHaveClass("primary");
  const toggle = section.getByRole("button", { name: m.commitMenu });
  expect(toggle).not.toHaveClass("primary");
  expect(toggle).toHaveAttribute("type", "button");
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  expect(section.queryByRole("button", { name: m.commitPush })).not.toBeInTheDocument();
  await userEvent.click(toggle);
  const item = within(panelOf(toggle)!).getByRole("button", { name: m.commitPush });
  expect(item).not.toHaveClass("primary");
  expect(item).toHaveAttribute("type", "button");
  expect(commit.closest("form")).toContainElement(item);
});

test.each([
  ["Escape", async () => userEvent.keyboard("{Escape}")],
  [
    "a press outside",
    async () => fireEvent.pointerDown(screen.getByRole("heading", { name: m.changesTitle })),
  ],
])("%s closes Commit's menu", async (_, close) => {
  repoApi({ "GET /api/git/status": status(STAGED, [], ELSEWHERE) });
  renderApp(`/?folder=${REPO}`);
  const section = await changes();
  const toggle = await section.findByRole("button", { name: m.commitMenu });
  await userEvent.click(toggle);
  expect(section.getByRole("button", { name: m.commitPush })).toBeInTheDocument(); // positive control
  await close();
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  expect(section.queryByRole("button", { name: m.commitPush })).not.toBeInTheDocument();
});

// --- the status line -----------------------------------------------------------

test("the status line says when the status was checked, and moves on by itself without asking again", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    const fake = repoApi();
    renderApp(`/?folder=${REPO}`);
    const section = await changes();
    expect(await section.findByText(m.checkedAgo(0))).toBeInTheDocument();
    const refresh = section.getByRole("button", { name: m.refreshStatus });
    expect(refresh.textContent).toBe(""); // an icon only
    expect(refresh.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
    const asked = fake.made("GET", "/api/git/status").length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(125_000);
    });
    expect(section.getByText(m.checkedAgo(125))).toBeInTheDocument();
    expect(section.queryByText(m.checkedAgo(0))).not.toBeInTheDocument();
    expect(fake.made("GET", "/api/git/status").length).toBe(asked);
    fireEvent.click(refresh);
    expect(await section.findByText(m.checkedAgo(0))).toBeInTheDocument();
    expect(fake.made("GET", "/api/git/status").length).toBe(asked + 1);
  } finally {
    vi.useRealTimers();
  }
});

test("a failed refresh keeps when the status was last checked, and shows the problem", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    let calls = 0;
    repoApi({
      "GET /api/git/status": () =>
        ++calls === 1
          ? status([], [{ path: "new.txt", change: "untracked" }])
          : problem(409, "git_failed", "planted refusal of the status", { git: [], git_more: 0 }),
    });
    renderApp(`/?folder=${REPO}`);
    const section = await changes();
    expect(await section.findByText(m.checkedAgo(0))).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(125_000);
    });
    fireEvent.click(section.getByRole("button", { name: m.refreshStatus }));
    expect(await section.findByRole("alert")).toHaveTextContent("planted refusal of the status");
    expect(section.getByText(m.checkedAgo(125))).toBeInTheDocument();
    expect(section.getByText("new.txt")).toBeInTheDocument(); // what was last known stays
  } finally {
    vi.useRealTimers();
  }
});

test("the status line's timer stops when the section goes", async () => {
  const set = vi.spyOn(globalThis, "setInterval");
  const cleared = vi.spyOn(globalThis, "clearInterval");
  try {
    repoApi();
    const app = renderApp(`/?folder=${REPO}`);
    expect(await (await changes()).findByText(m.checkedAgo(0))).toBeInTheDocument();
    const ticks = set.mock.calls
      .map((call, i) => [call[1], set.mock.results[i].value] as const)
      .filter(([ms]) => ms === CHECKED_TICK_MS)
      .map(([, id]) => id);
    expect(ticks).toHaveLength(1); // positive control: one was set
    expect(cleared).not.toHaveBeenCalledWith(ticks[0]);
    await app.navigate("/?folder=projects/plain");
    await screen.findByRole("region", { name: "projects/plain" });
    expect(screen.queryByRole("region", { name: m.changesTitle })).not.toBeInTheDocument();
    expect(cleared).toHaveBeenCalledWith(ticks[0]);
  } finally {
    set.mockRestore();
    cleared.mockRestore();
  }
});

// --- what does not wait for the status ----------------------------------------------

test("when the status cannot be had, the branches can still be switched, made and refreshed", async () => {
  let calls = 0;
  repoApi({
    "GET /api/git/status": () =>
      ++calls === 1
        ? problem(409, "git_failed", "planted refusal of the status", { git: [], git_more: 0 })
        : status(STAGED, [], ELSEWHERE),
  });
  renderApp(`/?folder=${REPO}`);
  const section = await changes();
  expect(await section.findByRole("alert")).toHaveTextContent("planted refusal of the status");
  const button = await lookup(); // named from the current local branch
  expect(button).toHaveTextContent("main");
  expect(section.getByRole("button", { name: m.newBranch })).toBeEnabled();
  expect(section.getByRole("button", { name: m.refreshBranches })).toBeEnabled();
  expect(section.queryByRole("button", { name: m.push })).not.toBeInTheDocument();
  expect(section.queryByTitle(m.tracking("origin/main", 1, 2))).not.toBeInTheDocument();
  const popup = await openLookup();
  expect(popup.getByRole("button", { name: m.switchTo("topic") })).toBeInTheDocument();
  await userEvent.keyboard("{Escape}");
  fireEvent.click(section.getByRole("button", { name: m.refreshStatus }));
  // positive control: once the status comes, what needs it is offered too
  expect(await section.findByRole("button", { name: m.push })).toBeInTheDocument();
  expect(section.getByTitle(m.tracking("origin/main", 1, 2))).toBeInTheDocument();
});

test("while the status is unknown and no local branch is current, the lookup is named for the branches", async () => {
  repoApi({
    "GET /api/git/status": problem(409, "git_failed", "planted refusal", { git: [], git_more: 0 }),
    "GET /api/git/branches": {
      ...BRANCHES,
      local: { branches: [{ name: "topic", current: false, upstream: null }], more: 0 },
    },
  });
  renderApp(`/?folder=${REPO}`);
  const button = await lookup(m.branchesTitle);
  expect(button).toHaveTextContent(m.branchesTitle);
});

// --- New branch while its request runs ---------------------------------------------

test("a refused new branch puts focus back in its field", async () => {
  repoApi({
    "POST /api/git/branch": problem(409, "git_failed", "git refused. What it said is below.", {
      git: ["exists"],
      git_more: 0,
    }),
  });
  renderApp(`/?folder=${REPO}`);
  const section = await changes();
  await userEvent.click(await section.findByRole("button", { name: m.newBranch }));
  const field = section.getByLabelText(m.newBranchName);
  await userEvent.type(field, "main");
  await userEvent.click(section.getByRole("button", { name: m.create }));
  expect(await section.findByRole("alert")).toHaveTextContent("git refused.");
  expect(field).toHaveFocus();
});

test("a new branch made after its form was left takes no focus", async () => {
  let answer: (value: unknown) => void = () => {};
  let release: () => void = () => {};
  let hold = false;
  const fake = repoApi({
    "POST /api/git/branch": () => new Promise((resolve) => (answer = resolve)),
    // the status asked for after the new branch is held, so that the + is enabled
    // again before what follows the request runs
    "GET /api/git/status": () =>
      hold
        ? new Promise((resolve) => (release = () => resolve(status())))
        : status([], [{ path: "new.txt", change: "untracked" }]),
  });
  renderApp(`/?folder=${REPO}`);
  const section = await changes();
  const plus = await section.findByRole("button", { name: m.newBranch });
  await userEvent.click(plus);
  await userEvent.type(section.getByLabelText(m.newBranchName), "topic/new");
  await userEvent.click(section.getByRole("button", { name: m.create }));
  await userEvent.keyboard("{Escape}");
  expect(plus).toHaveAttribute("aria-expanded", "false"); // left while it ran
  const elsewhere = section.getByRole("button", { name: m.refreshStatus });
  act(() => elsewhere.focus());
  hold = true;
  const asked = fake.made("GET", "/api/git/status").length;
  await act(async () => answer({ path: REPO, branch: "topic/new" }));
  await waitFor(() => expect(fake.made("GET", "/api/git/status").length).toBeGreaterThan(asked));
  await waitFor(() => expect(plus).toBeEnabled()); // the + could take focus now
  hold = false;
  await act(async () => release());
  await act(async () => new Promise((resolve) => setTimeout(resolve, 20)));
  expect(elsewhere).toHaveFocus();
  // positive control: what follows the request ran, as the name it emptied shows
  await userEvent.click(plus);
  expect(section.getByLabelText(m.newBranchName)).toHaveValue("");
});

test("a refusal that arrives after New branch was closed is not shown when it opens again", async () => {
  let answer: (value: unknown) => void = () => {};
  repoApi({ "POST /api/git/branch": () => new Promise((resolve) => (answer = resolve)) });
  renderApp(`/?folder=${REPO}`);
  const section = await changes();
  const plus = await section.findByRole("button", { name: m.newBranch });
  await userEvent.click(plus);
  await userEvent.type(section.getByLabelText(m.newBranchName), "main");
  await userEvent.click(section.getByRole("button", { name: m.create }));
  await userEvent.keyboard("{Escape}");
  await act(async () =>
    answer(problem(409, "git_failed", "planted late refusal", { git: ["exists"], git_more: 0 })),
  );
  await waitFor(() => expect(plus).toBeEnabled());
  await userEvent.click(plus);
  expect(section.getByLabelText(m.newBranchName)).toBeInTheDocument(); // positive control: open
  expect(section.queryByText("planted late refusal")).not.toBeInTheDocument();
});

test("while a request runs, New branch and Commit's menu wait too", async () => {
  let answer: (value: unknown) => void = () => {};
  repoApi({
    "GET /api/git/status": status(STAGED, [{ path: "b.txt", change: "untracked" }], ELSEWHERE),
    "POST /api/git/stage": () => new Promise((resolve) => (answer = resolve)),
  });
  renderApp(`/?folder=${REPO}`);
  const section = await changes();
  const plus = await section.findByRole("button", { name: m.newBranch });
  const toggle = section.getByRole("button", { name: m.commitMenu });
  expect(plus).toBeEnabled(); // positive controls: offered while nothing runs
  expect(toggle).toBeEnabled();
  const notStaged = await list(m.notStagedTitle);
  await userEvent.click(notStaged.getByRole("checkbox", { name: /b\.txt/ }));
  await userEvent.click(notStaged.getByRole("button", { name: m.stageSelected }));
  expect(plus).toBeDisabled();
  expect(toggle).toBeDisabled();
  await act(async () => answer({ path: REPO }));
  await waitFor(() => expect(plus).toBeEnabled());
});

// --- what is focused, closed and listed again ------------------------------------------

test("the lookup puts focus in its filter as it opens", async () => {
  repoApi();
  renderApp(`/?folder=${REPO}`);
  const popup = await openLookup();
  expect(popup.getByRole("textbox", { name: m.findBranch })).toHaveFocus();
});

test("New branch puts focus in its field as it opens", async () => {
  repoApi();
  renderApp(`/?folder=${REPO}`);
  const section = await changes();
  await userEvent.click(await section.findByRole("button", { name: m.newBranch }));
  expect(section.getByLabelText(m.newBranchName)).toHaveFocus();
});

test("Commit & Push closes Commit's menu", async () => {
  const fake = repoApi({
    "GET /api/git/status": status(STAGED, [], ELSEWHERE),
    "POST /api/git/commit-push": { path: REPO, commit: "abc", push: PUSHED, push_problem: null },
  });
  renderApp(`/?folder=${REPO}`);
  const section = await changes();
  const toggle = await section.findByRole("button", { name: m.commitMenu });
  await userEvent.click(toggle);
  expect(toggle).toHaveAttribute("aria-expanded", "true"); // positive control
  await userEvent.click(section.getByRole("button", { name: m.commitPush }));
  await waitFor(() => expect(fake.made("POST", "/api/git/commit-push")).toHaveLength(1));
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  expect(section.queryByRole("button", { name: m.commitPush })).not.toBeInTheDocument();
});

test("the branches are listed again when the current branch changes, and not on a refresh that keeps it", async () => {
  let name = "main";
  const fake = repoApi({
    "GET /api/git/status": () =>
      status([], [], {
        branch: { name, commit: "0123456789abcdef", upstream: null, ahead: null, behind: null },
      }),
  });
  renderApp(`/?folder=${REPO}`);
  const section = await changes();
  await lookup();
  await waitFor(() => expect(fake.made("GET", "/api/git/branches").length).toBeGreaterThan(0));
  const listed = () => fake.made("GET", "/api/git/branches").length;
  const before = listed();
  const statuses = fake.made("GET", "/api/git/status").length;
  fireEvent.click(section.getByRole("button", { name: m.refreshStatus }));
  await waitFor(() => expect(fake.made("GET", "/api/git/status").length).toBeGreaterThan(statuses));
  await act(async () => new Promise((resolve) => setTimeout(resolve, 20)));
  expect(listed()).toBe(before); // the same branch: not listed again
  name = "topic"; // as when Claude switches
  fireEvent.click(section.getByRole("button", { name: m.refreshStatus }));
  expect(await lookup(m.onBranch("topic"))).toBeInTheDocument();
  await waitFor(() => expect(listed()).toBeGreaterThan(before));
});

test("files coming to an empty list open it in the very render they are drawn in", () => {
  // What the page shows at each commit, read during it: a list drawn with its
  // files but closed, even for one frame, is seen here.
  const seen: { open: string | null; files: number }[] = [];
  function Witness() {
    useLayoutEffect(() => {
      seen.push({
        open: document.querySelector("h4 button")!.getAttribute("aria-expanded"),
        files: document.querySelectorAll('li input[type="checkbox"]').length,
      });
    });
    return null;
  }
  const none = new Set<string>();
  const shown = (files: Files) => (
    <>
      <FileList
        title={m.stagedTitle}
        list={{ files, more: 0 }}
        selected={none}
        onSelect={() => {}}
        action={m.unstageSelected}
        busy={false}
        onAct={() => {}}
      />
      <Witness />
    </>
  );
  const { rerender } = render(shown([]));
  expect(seen.at(-1)).toEqual({ open: "false", files: 0 }); // closed while empty
  rerender(shown([{ path: "a.txt", change: "added" }]));
  const drawn = seen.filter((s) => s.files > 0);
  expect(drawn.length).toBeGreaterThan(0); // positive control: the files were drawn
  expect(drawn.map((s) => s.open)).toEqual(drawn.map(() => "true"));
});

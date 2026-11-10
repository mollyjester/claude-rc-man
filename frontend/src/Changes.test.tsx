import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

import { Changes } from "./Changes";
import { m } from "./messages";
import { fakeApi, problem, type Routes } from "./test/fakeApi";
import { renderAlone } from "./test/render";

// The Changes section is rendered on its own, with the Settings page its links
// lead to; Dashboard.test.tsx shows it in a workspace's panel.
function renderChanges() {
  return renderAlone(<Changes workspace={WORKSPACE} pollMs={0} />, ["/settings"]);
}

const WORKSPACE = 7;
type Files = {
  path: string;
  change: string;
  from?: string;
  submodule?: boolean;
  selectable?: boolean;
}[];

function status(staged: Files = [], notStaged: Files = [], extra: Record<string, unknown> = {}) {
  return {
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

test("the Changes section asks git about its repository", async () => {
  const fake = repoApi();
  renderChanges();
  expect(await changes()).toBeTruthy();
  await waitFor(() => expect(fake.made("GET", "/api/git/branches").length).toBeGreaterThan(0));
  expect(fake.made("GET", "/api/git/status")[0].query.get("workspace")).toBe(String(WORKSPACE));
  expect(fake.made("GET", "/api/git/branches")[0].query.get("workspace")).toBe(String(WORKSPACE));
});

test("a workspace that is not a repository says so quietly, with no alert and no branch bar", async () => {
  const fake = fakeApi({
    "GET /api/git/status": problem(409, "not_a_repository", "planted: not a repository"),
    "GET /api/git/branches": problem(409, "not_a_repository", "planted: not a repository"),
  });
  renderChanges();
  const section = await changes();
  expect(await section.findByText(m.notARepository)).toBeInTheDocument();
  await waitFor(() => expect(fake.made("GET", "/api/git/branches").length).toBeGreaterThan(0));
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(section.queryByText("planted: not a repository")).not.toBeInTheDocument();
  expect(section.queryByRole("button", { name: m.newBranch })).not.toBeInTheDocument();
  expect(section.queryByText(m.loading)).not.toBeInTheDocument();
  expect(section.getByRole("button", { name: m.refreshStatus })).toBeInTheDocument();
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
  renderChanges();
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
      return {};
    },
  });
  renderChanges();
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
    workspace: WORKSPACE,
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
    "POST /api/git/unstage": {},
  });
  renderChanges();
  const staged = await list(m.stagedTitle);
  const button = staged.getByRole("button", { name: m.unstageSelected });
  expect(button).toBeDisabled();
  await userEvent.click(staged.getByRole("checkbox", { name: /b\.txt/ }));
  await userEvent.click(button);
  await waitFor(() => expect(fake.made("POST", "/api/git/unstage")).toHaveLength(1));
  expect(fake.made("POST", "/api/git/unstage")[0].body).toEqual({
    workspace: WORKSPACE,
    files: ["b.txt"],
  });
});

test("Commit sends the message and empties the box", async () => {
  const fake = repoApi({
    "GET /api/git/status": status([{ path: "a.txt", change: "added" }]),
    "POST /api/git/commit": { commit: "abc", branch: "main" },
  });
  renderChanges();
  const box = await (await changes()).findByLabelText(m.commitMessage);
  await userEvent.type(box, "Add a.txt{enter}With a body");
  const commit = (await changes()).getByRole("button", { name: m.commit });
  expect(commit).toHaveClass("primary");
  await userEvent.click(commit);
  await waitFor(() => expect(fake.made("POST", "/api/git/commit")).toHaveLength(1));
  expect(fake.made("POST", "/api/git/commit")[0].body).toEqual({
    workspace: WORKSPACE,
    message: "Add a.txt\nWith a body",
  });
  await waitFor(() => expect(box).toHaveValue(""));
});

test("the branch bar names the branch, and shows how far it is from its upstream", async () => {
  repoApi();
  renderChanges();
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

test("Switch from the lookup sends the branch, whether it is remote, and closes the lookup", async () => {
  const fake = repoApi({ "POST /api/git/switch": { branch: "x" } });
  renderChanges();
  let popup = await openLookup();
  await userEvent.click(popup.getByRole("button", { name: m.switchTo("topic") }));
  await waitFor(() => expect(fake.made("POST", "/api/git/switch")).toHaveLength(1));
  expect(fake.made("POST", "/api/git/switch")[0].body).toEqual({
    workspace: WORKSPACE,
    branch: "topic",
    remote: false,
  });
  const button = await lookup();
  expect(button).toHaveAttribute("aria-expanded", "false");
  popup = await openLookup();
  await userEvent.click(popup.getByRole("button", { name: m.switchTo("origin/feature") }));
  await waitFor(() => expect(fake.made("POST", "/api/git/switch")).toHaveLength(2));
  expect(fake.made("POST", "/api/git/switch")[1].body).toEqual({
    workspace: WORKSPACE,
    branch: "origin/feature",
    remote: true,
  });
  expect(await lookup()).toHaveAttribute("aria-expanded", "false");
});

test("New branch is a + that opens a form; Create sends the name and closes it", async () => {
  const fake = repoApi({ "POST /api/git/branch": { branch: "topic/new" } });
  renderChanges();
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
  expect(fake.made("POST", "/api/git/branch")[0].body).toEqual({
    workspace: WORKSPACE,
    name: "topic/new",
  });
  await waitFor(() => expect(section.queryByLabelText(m.newBranchName)).not.toBeInTheDocument());
  expect(plus).toHaveAttribute("aria-expanded", "false");
});

test("Refresh branches fetches, lists them again, and waits while it runs", async () => {
  let answer: (value: unknown) => void = () => {};
  const fake = repoApi({
    "POST /api/git/fetch": () => new Promise((resolve) => (answer = resolve)),
  });
  renderChanges();
  const refresh = (await bar()).getByRole("button", { name: m.refreshBranches });
  expect(refresh.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  await waitFor(() => expect(fake.made("GET", "/api/git/branches").length).toBeGreaterThan(0));
  expect(refresh).toBeEnabled();
  expect(refresh).not.toHaveClass("spinning");
  const listed = fake.made("GET", "/api/git/branches").length;
  await userEvent.click(refresh);
  await waitFor(() => expect(fake.made("POST", "/api/git/fetch")).toHaveLength(1));
  expect(fake.made("POST", "/api/git/fetch")[0].body).toEqual({ workspace: WORKSPACE });
  expect(refresh).toBeDisabled();
  expect(refresh).toHaveClass("spinning");
  await act(async () => answer({}));
  await waitFor(() => expect(refresh).toBeEnabled());
  expect(refresh).not.toHaveClass("spinning");
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
  renderChanges();
  const popup = await openLookup();
  await userEvent.click(popup.getByRole("button", { name: m.switchTo("topic") }));
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("git refused.");
  expect(within(alert).getByLabelText(m.gitSaid)).toHaveTextContent(
    /would be overwritten\s+Aborting/,
  );
  expect(alert).toHaveTextContent(m.andMoreLines(3));
});

// --- push ----------------------------------------------------------------------

const STAGED: Files = [{ path: "a.txt", change: "added" }];
const PUSHED = { branch: "main", upstream: "origin/main", upstream_set: false };
// What git-status says of a push: where it goes, and for GitHub whether a token is stored.
const ELSEWHERE = { push_to_github: false, github_token: null };

test("Push pushes the branch, and says where it went", async () => {
  const fake = repoApi({
    "GET /api/git/status": status(STAGED, [], ELSEWHERE),
    "POST /api/git/push": PUSHED,
  });
  renderChanges();
  const section = await changes();
  const push = await section.findByRole("button", { name: m.push });
  await userEvent.click(push);
  await waitFor(() => expect(fake.made("POST", "/api/git/push")).toHaveLength(1));
  expect(fake.made("POST", "/api/git/push")[0].body).toEqual({ workspace: WORKSPACE });
  expect(await section.findByText(m.pushed("main", "origin/main"))).toBeInTheDocument();
});

test("Commit & Push sends the message, says what was pushed, and empties the box", async () => {
  const fake = repoApi({
    "GET /api/git/status": status(STAGED, [], ELSEWHERE),
    "POST /api/git/commit-push": {
      commit: "abcdef1234",
      push: PUSHED,
      push_problem: null,
    },
  });
  renderChanges();
  const section = await changes();
  const box = await section.findByLabelText(m.commitMessage);
  await userEvent.type(box, "Add a.txt");
  await commitAndPush();
  await waitFor(() => expect(fake.made("POST", "/api/git/commit-push")).toHaveLength(1));
  expect(fake.made("POST", "/api/git/commit-push")[0].body).toEqual({
    workspace: WORKSPACE,
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
  renderChanges();
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

// --- the status line -----------------------------------------------------------

test("the status line says when the status was checked, and moves on by itself without asking again", async () => {
  vi.useFakeTimers();
  try {
    const fake = repoApi();
    renderChanges();
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

// --- sync ----------------------------------------------------------------------

// A branch a sync can run on: ahead of its upstream and not behind it, so not diverged.
const AHEAD = {
  name: "main",
  commit: "0123456789abcdef",
  upstream: "origin/main",
  ahead: 1,
  behind: 0,
};
const SYNCABLE = status([], [], { branch: AHEAD, ...ELSEWHERE });
const SYNCED = {
  branch: "main",
  upstream: "origin/main",
  pulled: true,
  pushed: true,
  upstream_set: false,
  push_problem: null,
};

test("Sync is offered where a sync can run: it posts the sync alone, waits while it runs, then lists again", async () => {
  let answer: (value: unknown) => void = () => {};
  const fake = repoApi({
    "GET /api/git/status": SYNCABLE,
    "POST /api/git/sync": () => new Promise((resolve) => (answer = resolve)),
  });
  renderChanges();
  const sync = await (await bar()).findByRole("button", { name: m.sync });
  expect(sync).toHaveAttribute("title", m.sync);
  expect(sync.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  expect(sync).toBeEnabled();
  expect(sync).not.toHaveClass("spinning");
  expect((await bar()).queryByRole("button", { name: m.refreshBranches })).not.toBeInTheDocument();
  const statuses = fake.made("GET", "/api/git/status").length;
  const listed = fake.made("GET", "/api/git/branches").length;
  await userEvent.click(sync);
  await waitFor(() => expect(fake.made("POST", "/api/git/sync")).toHaveLength(1));
  expect(fake.made("POST", "/api/git/sync")[0].body).toEqual({ workspace: WORKSPACE });
  expect(sync).toBeDisabled();
  expect(sync).toHaveClass("spinning");
  await act(async () => answer(SYNCED));
  await waitFor(() => expect(sync).toBeEnabled());
  expect(sync).not.toHaveClass("spinning");
  await waitFor(() => expect(fake.made("GET", "/api/git/branches").length).toBeGreaterThan(listed));
  expect(fake.made("GET", "/api/git/status").length).toBeGreaterThan(statuses);
  expect(fake.made("POST", "/api/git/fetch")).toHaveLength(0);
});

// --- the changes' letters and words -----------------------------------------------

// The changes that have a word but no letter, or a letter but no word.
function unpaired(words: Record<string, string>, letters: Record<string, string>): string[] {
  return [
    ...Object.keys(words)
      .filter((code) => !(code in letters))
      .map((code) => `${code}: no letter`),
    ...Object.keys(letters)
      .filter((code) => !(code in words))
      .map((code) => `${code}: no word`),
  ];
}

// The letters two or more changes share.
function shared(letters: Record<string, string>): string[] {
  const all = Object.values(letters);
  return [...new Set(all.filter((letter, i) => all.indexOf(letter) !== i))];
}

test("every change has both a letter and a word, and the letters differ", () => {
  expect(Object.keys(m.changes).length).toBeGreaterThan(0); // non-vacuity
  expect(unpaired(m.changes, m.changeLetters)).toEqual([]);
  expect(shared(m.changeLetters)).toEqual([]);
});

test("the checks fail on two changes that share a letter, and on a change with only a word", () => {
  const [first, second] = Object.keys(m.changeLetters);
  const planted = { ...m.changeLetters, [second]: m.changeLetters[first] };
  expect(planted[second]).toBe(planted[first]); // the plant took effect
  expect(shared(planted)).toEqual([m.changeLetters[first]]);
  expect(unpaired({ ...m.changes, planted: "Planted" }, m.changeLetters)).toEqual([
    "planted: no letter",
  ]);
});

import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

import { downloadUrl } from "./api";
import { browser } from "./browser";
import { formatBytes } from "./format";
import { m } from "./messages";
import { fakeApi, ME, problem, type Routes } from "./test/fakeApi";
import { renderApp } from "./test/render";
import { turn } from "./test/turn";

// A ready workspace, as /api/workspaces lists one.
const ALPHA = {
  id: 1,
  name: "alpha",
  device: "rcman-ws-1",
  state: "ready",
  error: null,
  source: null,
  source_page: null,
  log: null,
  server: null,
  server_error: null,
  claude_version: null,
  rebuild: { state: null, log: null, error: null },
  rebuildable: false,
  ports: [],
  stoppable: false,
  startable: false,
  stopping: false,
  starting: false,
};
const FILES = "/api/workspaces/1/files";
const NOW = 1_800_000_000;

// The folder work, as the server lists it: folders first.
const WORK = {
  path: "work",
  entries: [
    { name: "node_modules", kind: "folder", size: null, changed: NOW - 7200, target: null },
    { name: "a.txt", kind: "file", size: 2048, changed: NOW - 300, target: null },
  ],
  more: 0,
  now: NOW,
};

function filesApi(extra: Routes = {}) {
  return fakeApi({
    "GET /api/me": ME,
    "GET /api/workspaces": { workspaces: [ALPHA], claude_approved: null },
    "GET /api/git/status": problem(409, "not_a_repository", "planted"),
    "GET /api/git/branches": problem(409, "not_a_repository", "planted"),
    [`GET ${FILES}`]: WORK,
    [`PUT ${FILES}`]: (call) => ({ name: call.query.get("name") }),
    ...extra,
  });
}

// The Files tab of alpha, showing work, once its rows are drawn.
async function filesOfWork() {
  const shown = renderApp("/?workspace=1&tab=files&path=work");
  const list = await screen.findByRole("list", { name: m.filesAt("work") });
  await within(list).findByText("a.txt");
  return { ...shown, list };
}

const fileInput = () => document.querySelector<HTMLInputElement>('input[type="file"]')!;
const file = (name: string) => new File([`planted ${name}`], name, { type: "text/plain" });

test("Download asks first, then starts the browser's download", async () => {
  const download = vi.spyOn(browser, "download").mockImplementation(() => {});
  try {
    const fake = filesApi({
      [`GET ${FILES}/download/check`]: { kind: "file", name: "a.txt", size: 2048 },
    });
    const { list } = await filesOfWork();
    await userEvent.click(within(list).getByRole("button", { name: m.filesDownload("a.txt") }));
    await waitFor(() => expect(download).toHaveBeenCalledTimes(1));
    expect(fake.made("GET", `${FILES}/download/check`)[0].query.get("path")).toBe("work/a.txt");
    expect(download).toHaveBeenCalledWith(downloadUrl(1, "work/a.txt"));
  } finally {
    download.mockRestore();
  }
});

test("a folder over the limit is refused beside its row, and nothing downloads", async () => {
  const download = vi.spyOn(browser, "download").mockImplementation(() => {});
  try {
    filesApi({
      [`GET ${FILES}/download/check`]: problem(413, "folder_too_big", "planted: too big", {
        size: 1_200_000_000,
        limit_mb: 500,
      }),
    });
    const { list } = await filesOfWork();
    const button = within(list).getByRole("button", {
      name: m.filesDownloadFolder("node_modules"),
    });
    await userEvent.click(button);
    const row = button.closest("li")!;
    const said = m.filesTooBig("node_modules", formatBytes(1_200_000_000), formatBytes(500e6));
    // as said, its sizes' non-breaking spaces among it
    expect((await within(row).findByRole("alert")).textContent).toBe(said);
    expect(button).toHaveFocus();
    expect(download).not.toHaveBeenCalled();
  } finally {
    download.mockRestore();
  }
});

test("a name already there is asked about first; Replace sends it with overwrite, the rest without, into the folder shown, and the folder is listed again", async () => {
  const fake = filesApi();
  await filesOfWork();
  const upload = screen.getByRole("button", { name: m.filesUpload });
  await userEvent.click(upload);
  await userEvent.upload(fileInput(), [file("a.txt"), file("b.txt")]);
  const dialog = await screen.findByRole("dialog", { name: m.filesReplaceTitle("a.txt") });
  expect(dialog).toHaveTextContent(m.filesReplaceBody("a.txt", "work"));
  expect(fake.made("PUT", FILES)).toHaveLength(0); // nothing is sent before the answer
  const listed = fake.made("GET", FILES).length;
  await userEvent.click(within(dialog).getByRole("button", { name: m.filesReplace }));
  await waitFor(() => expect(fake.made("PUT", FILES)).toHaveLength(2));
  const sent = fake
    .made("PUT", FILES)
    .map((c) => [
      c.query.get("path"),
      c.query.get("name"),
      c.query.get("overwrite"),
      (c.body as File).name,
    ]);
  expect(sent).toEqual([
    ["work", "a.txt", "true", "a.txt"],
    ["work", "b.txt", "false", "b.txt"],
  ]);
  expect(await screen.findByText(m.filesUploadedCount(2, 2))).toBeInTheDocument();
  await waitFor(() => expect(fake.made("GET", FILES).length).toBeGreaterThan(listed));
  expect(upload).toHaveFocus();
});

test("dropping files on the list uploads them", async () => {
  const fake = filesApi();
  const { list } = await filesOfWork();
  const dropped = { files: [file("c.txt")], types: ["Files"] };
  fireEvent.dragEnter(list, { dataTransfer: dropped });
  expect(await screen.findByText(m.filesDrop("work"))).toBeInTheDocument();
  fireEvent.drop(list, { dataTransfer: dropped });
  await waitFor(() => expect(fake.made("PUT", FILES)).toHaveLength(1));
  expect(fake.made("PUT", FILES)[0].query.get("name")).toBe("c.txt");
  expect(await screen.findByText(m.filesUploaded("c.txt"))).toBeInTheDocument();
});

test("a name the listing did not show that is already there is asked about after the others, and Replace sends it again with overwrite", async () => {
  const fake = filesApi({
    // b.txt fails, and so does unlisted.txt once it is sent to replace the one there
    [`PUT ${FILES}`]: (call) =>
      call.query.get("name") === "unlisted.txt" && call.query.get("overwrite") === "false"
        ? problem(409, "file_exists", "planted: there")
        : problem(502, "transfer_failed", "planted: not passed"),
  });
  await filesOfWork();
  await userEvent.upload(fileInput(), [file("unlisted.txt"), file("b.txt")]);
  const dialog = await screen.findByRole("dialog", { name: m.filesReplaceTitle("unlisted.txt") });
  const said = () => screen.queryAllByRole("alert").map((bar) => bar.textContent);
  expect(said()).toEqual([expect.stringContaining("b.txt")]); // unlisted.txt asked, not refused
  await userEvent.click(within(dialog).getByRole("button", { name: m.filesReplace }));
  await waitFor(() => expect(fake.made("PUT", FILES)).toHaveLength(3));
  expect(
    fake.made("PUT", FILES).map((c) => [c.query.get("name"), c.query.get("overwrite")]),
  ).toEqual([
    ["unlisted.txt", "false"],
    ["b.txt", "false"],
    ["unlisted.txt", "true"],
  ]);
  // the second round's failure is told beside the first's
  await waitFor(() =>
    expect(said()).toEqual([
      expect.stringContaining(m.filesUploadFailed("b.txt")),
      expect.stringContaining(m.filesUploadFailed("unlisted.txt")),
    ]),
  );
});

test("an upload that ends after another folder is opened leaves that folder shown", async () => {
  let finish = () => {};
  const TOP = { path: "", entries: [], more: 0, now: NOW, reserved: [] };
  filesApi({
    [`GET ${FILES}`]: (call) => (call.query.get("path") === "" ? TOP : WORK),
    [`PUT ${FILES}`]: (call) =>
      new Promise((resolve) => {
        finish = () => resolve({ name: call.query.get("name") });
      }),
  });
  await filesOfWork();
  await userEvent.upload(fileInput(), [file("c.txt")]);
  const upload = screen.getByRole("button", { name: m.filesUpload });
  await waitFor(() => expect(upload).toHaveAttribute("aria-disabled", "true"));
  const crumbs = screen.getByRole("navigation", { name: m.filesPath });
  await userEvent.click(within(crumbs).getByRole("link", { name: m.filesTop }));
  await screen.findByRole("list", { name: m.filesAt("") });
  finish();
  await waitFor(() => expect(upload).toHaveAttribute("aria-disabled", "false"));
  await turn();
  expect(screen.getByRole("list", { name: m.filesAt("") })).toBeInTheDocument();
  expect(screen.queryByRole("list", { name: m.filesAt("work") })).toBeNull();
  expect(screen.queryByText(m.filesUploaded("c.txt"))).toBeNull();
});

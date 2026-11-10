import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ADMIN } from "./AdminPages";
import { m } from "./messages";
import { ADMIN_USER, fakeApi, NOTHING_TO_ATTEND, type Reply, type Routes } from "./test/fakeApi";
import { renderApp } from "./test/render";

const PATH = ADMIN.settings.path;

// What GET /api/monitor/settings and GET /api/forwarding/settings answer; their defaults and
// bounds are the server's (backend/rcman/monitor.py, backend/rcman/forwarding_settings.py).
const MONITORING = {
  usage_interval_seconds: 60,
  disk_interval_seconds: 3600,
  host: true,
  people: true,
  containers: true,
  storage: true,
};
const FORWARDING = { enabled: false, idle_minutes: 5, absolute_hours: 12 };

// A reply held until the test releases it.
function held(reply: Reply) {
  let release = () => {};
  const out = new Promise<void>((resolve) => {
    release = resolve;
  });
  return {
    reply: async () => {
      await out;
      return reply;
    },
    release: () => release(),
  };
}

function api(extra: Routes = {}) {
  return fakeApi({
    "GET /api/me": ADMIN_USER,
    "GET /api/attention": NOTHING_TO_ATTEND,
    "GET /api/monitor/settings": { settings: MONITORING },
    "GET /api/forwarding/settings": { settings: FORWARDING },
    "PUT /api/monitor/settings": (call) => ({ settings: call.body }),
    "PUT /api/forwarding/settings": (call) => ({ settings: call.body }),
    ...extra,
  });
}

const card = (name: string) => screen.findByRole("region", { name });
const monitoring = () => card(m.monitorSettingsTitle);
const forwarding = () => card(m.forwardingTitle);
const field = async (part: Promise<HTMLElement>, label: string) =>
  within(await part).findByLabelText(label) as Promise<HTMLInputElement>;
const statusOf = (part: HTMLElement) => part.querySelector('[role="status"]') as HTMLElement;

// --- monitoring, moved from the Monitor page ---

test("the monitoring form shows the settings saved, two whole numbers in seconds and the four scopes", async () => {
  api();
  renderApp(PATH);
  const usage = await field(monitoring(), m.monitorUsageInterval);
  const disk = await field(monitoring(), m.monitorDiskInterval);
  expect(usage).toHaveValue("60");
  expect(disk).toHaveValue("3600");
  for (const input of [usage, disk]) {
    expect(input).toHaveAttribute("type", "text");
    expect(input).toHaveAttribute("inputmode", "numeric");
    expect(input).toHaveAttribute("autocomplete", "off");
  }
  expect(usage).toHaveAttribute("name", "usage_interval_seconds");
  expect(disk).toHaveAttribute("name", "disk_interval_seconds");
  expect(usage).toHaveAccessibleDescription(m.monitorUsageIntervalHelp);
  expect(disk).toHaveAccessibleDescription(m.monitorDiskIntervalHelp);
  const part = await monitoring();
  expect(within(part).getByRole("group", { name: m.monitorCollect })).toBeInTheDocument();
  for (const [name, label] of [
    ["host", m.monitorHost],
    ["people", m.monitorPeople],
    ["containers", m.monitorContainers],
    ["storage", m.monitorStorage],
  ]) {
    const box = within(part).getByRole("checkbox", { name: label });
    expect(box).toBeChecked();
    expect(box).toHaveAttribute("name", name);
    // the label is the target: a press on its words toggles the box
    await userEvent.click(within(part).getByText(label));
    expect(box).not.toBeChecked();
  }
});

test("Save sends all six monitoring settings, the intervals as whole numbers, and says they were saved", async () => {
  const fake = api();
  renderApp(PATH);
  const usage = await field(monitoring(), m.monitorUsageInterval);
  await userEvent.clear(usage);
  await userEvent.type(usage, "120");
  const part = await monitoring();
  await userEvent.click(within(part).getByRole("checkbox", { name: m.monitorHost }));
  await userEvent.click(within(part).getByRole("button", { name: m.monitorSave }));
  await waitFor(() => expect(fake.made("PUT", "/api/monitor/settings")).toHaveLength(1));
  expect(fake.made("PUT", "/api/monitor/settings")[0].body).toEqual({
    usage_interval_seconds: 120,
    disk_interval_seconds: 3600,
    host: false,
    people: true,
    containers: true,
    storage: true,
  });
  await waitFor(() => expect(statusOf(part)).toHaveTextContent(m.monitorSaved));
  // the other card saved nothing and says nothing
  expect(fake.made("PUT", "/api/forwarding/settings")).toHaveLength(0);
  await within(await forwarding()).findByRole("button", { name: m.forwardingSave });
  expect(statusOf(await forwarding())).toBeEmptyDOMElement();
});

// --- port forwarding ---

test("the port forwarding form shows the switch, and the timeouts in the units of their labels", async () => {
  api();
  renderApp(PATH);
  const part = await forwarding();
  const box = await within(part).findByRole("checkbox", { name: m.forwardingEnabled });
  expect(box).not.toBeChecked();
  expect(box).toHaveAttribute("name", "enabled");
  expect(box).toHaveAccessibleDescription(m.forwardingEnabledHelp);
  const idle = await field(forwarding(), m.forwardingIdle);
  const absolute = await field(forwarding(), m.forwardingAbsolute);
  expect(idle).toHaveValue("5");
  expect(absolute).toHaveValue("12");
  expect(idle).toHaveAttribute("name", "idle_minutes");
  expect(absolute).toHaveAttribute("name", "absolute_hours");
  expect(idle).toHaveAccessibleDescription(m.forwardingIdleHelp);
  expect(absolute).toHaveAccessibleDescription(m.forwardingAbsoluteHelp);
  for (const input of [idle, absolute]) {
    expect(input).toHaveAttribute("type", "text");
    expect(input).toHaveAttribute("inputmode", "numeric");
    expect(input).toHaveAttribute("autocomplete", "off");
  }
  // the label is the target: a press on its words toggles the box
  await userEvent.click(within(part).getByText(m.forwardingEnabled));
  expect(box).toBeChecked();
});

test("Save sends the three forwarding settings, shows Saving… while it is out, and says they were saved", async () => {
  const answer = held({ settings: { enabled: true, idle_minutes: 30, absolute_hours: 12 } });
  const fake = api({ "PUT /api/forwarding/settings": answer.reply });
  renderApp(PATH);
  const part = await forwarding();
  await userEvent.click(await within(part).findByRole("checkbox", { name: m.forwardingEnabled }));
  const idle = await field(forwarding(), m.forwardingIdle);
  await userEvent.clear(idle);
  await userEvent.type(idle, "30");
  const button = within(part).getByRole("button", { name: m.forwardingSave });
  await userEvent.click(button);
  await waitFor(() => expect(fake.made("PUT", "/api/forwarding/settings")).toHaveLength(1));
  expect(fake.made("PUT", "/api/forwarding/settings")[0].body).toEqual({
    enabled: true,
    idle_minutes: 30,
    absolute_hours: 12,
  });
  // while it is out: the card's live region says so, and the button cannot be pressed again;
  // its name holds the words it shows
  expect(statusOf(part)).toHaveTextContent(m.saving);
  expect(button).toBeDisabled();
  expect(button).toHaveTextContent(m.save);
  expect(m.forwardingSave).toContain(m.save);
  expect(part.querySelector("form")).toHaveAttribute("aria-busy", "true");
  answer.release();
  await waitFor(() => expect(statusOf(part)).toHaveTextContent(m.forwardingSaved));
  expect(button).toBeEnabled();
  // the other card saved nothing and says nothing
  expect(fake.made("PUT", "/api/monitor/settings")).toHaveLength(0);
  await within(await monitoring()).findByRole("button", { name: m.monitorSave });
  expect(statusOf(await monitoring())).toBeEmptyDOMElement();
});

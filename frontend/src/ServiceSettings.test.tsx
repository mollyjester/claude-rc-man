import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ADMIN } from "./AdminPages";
import { m } from "./messages";
import {
  ADMIN_USER,
  fakeApi,
  ME,
  NOTHING_TO_ATTEND,
  problem,
  type Reply,
  type Routes,
} from "./test/fakeApi";
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

test("the page is headed Service settings, is offered to administrators, and has a card for each part", async () => {
  api();
  renderApp(PATH);
  expect(
    await screen.findByRole("heading", { level: 1, name: m.serviceSettingsTitle }),
  ).toBeInTheDocument();
  expect(m.serviceSettingsTitle).not.toBe(m.navSettings); // not the person's own Settings
  expect(ADMIN.settings.label).toBe(m.navServiceSettings);
  const page = (await monitoring()).parentElement as HTMLElement;
  expect(page).toHaveClass("cards");
  for (const [title, line] of [
    [m.monitorSettingsTitle, m.monitorSettingsLine],
    [m.forwardingTitle, m.forwardingLine],
  ]) {
    const part = await card(title);
    expect(part).toHaveClass("card");
    expect(within(part).getByRole("heading", { level: 2, name: title })).toBeInTheDocument();
    expect(within(part).getByText(line)).toBeInTheDocument();
    // its own one primary button, Save
    await within(part).findByRole("button", { name: /Save/ });
    const primary = part.querySelectorAll("button.primary");
    expect(primary).toHaveLength(1);
    expect(primary[0]).toHaveTextContent(m.save);
    expect(part.querySelector("form")).toHaveAttribute("novalidate");
  }
});

test("people who are not administrators are sent on from the Service settings page", async () => {
  const fake = api({ "GET /api/me": ME });
  const { location } = renderApp(PATH);
  await screen.findByRole("region", { name: m.navDashboard }); // the Workspaces page
  expect(location().pathname).toBe("/");
  expect(fake.made("GET", "/api/monitor/settings")).toHaveLength(0);
  expect(fake.made("GET", "/api/forwarding/settings")).toHaveLength(0);
});

// --- monitoring, moved from the Monitor page ---

test("the monitoring form shows the settings saved, two whole numbers in seconds and the three scopes", async () => {
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
  ]) {
    const box = within(part).getByRole("checkbox", { name: label });
    expect(box).toBeChecked();
    expect(box).toHaveAttribute("name", name);
    // the label is the target: a press on its words toggles the box
    await userEvent.click(within(part).getByText(label));
    expect(box).not.toBeChecked();
  }
});

test("Save sends all five monitoring settings, the intervals as whole numbers, and says they were saved", async () => {
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
  });
  await waitFor(() => expect(statusOf(part)).toHaveTextContent(m.monitorSaved));
  // the other card saved nothing and says nothing
  expect(fake.made("PUT", "/api/forwarding/settings")).toHaveLength(0);
  await within(await forwarding()).findByRole("button", { name: m.forwardingSave });
  expect(statusOf(await forwarding())).toBeEmptyDOMElement();
});

test("what is not a whole number is sent as typed, for the server to judge", async () => {
  const fake = api({
    "PUT /api/monitor/settings": problem(422, "validation_failed", "Check the fields.", {
      errors: [{ field: "usage_interval_seconds", message: "planted: not a whole number" }],
    }),
  });
  renderApp(PATH);
  const usage = await field(monitoring(), m.monitorUsageInterval);
  await userEvent.clear(usage);
  await userEvent.type(usage, "1.5");
  await userEvent.click(within(await monitoring()).getByRole("button", { name: m.monitorSave }));
  await waitFor(() => expect(fake.made("PUT", "/api/monitor/settings")).toHaveLength(1));
  expect(fake.made("PUT", "/api/monitor/settings")[0].body).toMatchObject({
    usage_interval_seconds: "1.5",
  });
});

test("a monitoring refusal is shown under each field it is about, focus on the first, and what was typed stays", async () => {
  api({
    "PUT /api/monitor/settings": problem(422, "validation_failed", "Check the fields.", {
      errors: [
        { field: "disk_interval_seconds", message: "planted: too long" },
        { field: "usage_interval_seconds", message: "planted: too short" },
        { field: "containers", message: "planted: not a choice" },
      ],
    }),
  });
  renderApp(PATH);
  const usage = await field(monitoring(), m.monitorUsageInterval);
  await userEvent.clear(usage);
  await userEvent.type(usage, "5");
  const part = await monitoring();
  await userEvent.click(within(part).getByRole("button", { name: m.monitorSave }));
  const containers = within(part).getByRole("checkbox", { name: m.monitorContainers });
  for (const [input, message] of [
    [usage, "planted: too short"],
    [await field(monitoring(), m.monitorDiskInterval), "planted: too long"],
    [containers, "planted: not a choice"],
  ] as const) {
    await waitFor(() => expect(input).toHaveAttribute("aria-invalid", "true"));
    expect(input).toHaveAccessibleDescription(expect.stringContaining(message));
  }
  // the first refused field in the form's order, whatever order the server named them in
  expect(usage).toHaveFocus();
  expect(usage).toHaveValue("5");
  expect(statusOf(part)).not.toHaveTextContent(m.monitorSaved);
});

test("with the monitoring settings not read, the card says why and offers no form", async () => {
  api({
    "GET /api/monitor/settings": problem(
      500,
      "internal",
      "planted: the settings could not be read",
    ),
  });
  renderApp(PATH);
  const part = await monitoring();
  expect(
    await within(part).findByText("planted: the settings could not be read"),
  ).toBeInTheDocument();
  expect(part.querySelector("form")).toBeNull();
  // the other card is not held up by it
  expect(await field(forwarding(), m.forwardingIdle)).toHaveValue("5");
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

test("a changed field takes back what the card said it saved", async () => {
  api();
  renderApp(PATH);
  const part = await forwarding();
  await userEvent.click(await within(part).findByRole("button", { name: m.forwardingSave }));
  await waitFor(() => expect(statusOf(part)).toHaveTextContent(m.forwardingSaved));
  await userEvent.type(await field(forwarding(), m.forwardingIdle), "0");
  expect(statusOf(part)).toBeEmptyDOMElement();
});

test("a forwarding refusal is shown under its field, focus on the first refused, and what was typed stays", async () => {
  const fake = api({
    "PUT /api/forwarding/settings": problem(422, "validation_failed", "Check the fields.", {
      errors: [
        { field: "absolute_hours", message: "planted: at most 168" },
        { field: "idle_minutes", message: "planted: at least 1" },
      ],
    }),
  });
  renderApp(PATH);
  const idle = await field(forwarding(), m.forwardingIdle);
  const absolute = await field(forwarding(), m.forwardingAbsolute);
  await userEvent.clear(idle);
  await userEvent.type(idle, "0");
  await userEvent.clear(absolute);
  await userEvent.type(absolute, "1.5");
  const part = await forwarding();
  await userEvent.click(within(part).getByRole("button", { name: m.forwardingSave }));
  await waitFor(() => expect(fake.made("PUT", "/api/forwarding/settings")).toHaveLength(1));
  // what is not a whole number is sent as typed, for the server to judge
  expect(fake.made("PUT", "/api/forwarding/settings")[0].body).toEqual({
    enabled: false,
    idle_minutes: 0,
    absolute_hours: "1.5",
  });
  for (const [input, message] of [
    [idle, "planted: at least 1"],
    [absolute, "planted: at most 168"],
  ] as const) {
    await waitFor(() => expect(input).toHaveAttribute("aria-invalid", "true"));
    expect(input).toHaveAccessibleDescription(expect.stringContaining(message));
    // the message is right under its field
    expect(input.closest(".field")).toHaveTextContent(message);
  }
  expect(idle).toHaveFocus();
  expect(idle).toHaveValue("0");
  expect(absolute).toHaveValue("1.5");
  expect(statusOf(part)).not.toHaveTextContent(m.forwardingSaved);
  // the alert is the short summary, since every field shows its own
  expect(within(part).getByRole("alert")).toHaveTextContent(m.problemCheckFields);
});

test("each card's live region is outside its form, beside the summary of a refusal, as a Settings part's is", async () => {
  api();
  renderApp(PATH);
  for (const part of [await monitoring(), await forwarding()]) {
    await within(part).findByRole("button", { name: /Save/ });
    const status = statusOf(part);
    expect(status.closest("form")).toBeNull();
    expect(status.parentElement).toHaveClass("setting-body");
    expect(part.querySelectorAll('[role="status"]')).toHaveLength(1);
  }
});

test("a refusal of the switch is shown under it, and focus goes to it", async () => {
  api({
    "PUT /api/forwarding/settings": problem(422, "validation_failed", "Check the fields.", {
      errors: [{ field: "enabled", message: "planted: true or false" }],
    }),
  });
  renderApp(PATH);
  const part = await forwarding();
  const box = await within(part).findByRole("checkbox", { name: m.forwardingEnabled });
  await userEvent.click(within(part).getByRole("button", { name: m.forwardingSave }));
  await waitFor(() => expect(box).toHaveAttribute("aria-invalid", "true"));
  expect(box).toHaveAccessibleDescription(expect.stringContaining("planted: true or false"));
  expect(box).toHaveAccessibleDescription(expect.stringContaining(m.forwardingEnabledHelp));
  expect(box.closest(".field")).toHaveTextContent("planted: true or false");
  expect(box).toHaveFocus();
});

test.each([
  [403, "admin_only", "planted: only administrators"],
  [500, "internal_error", "planted: something went wrong"],
])(
  "a refusal about no field (%i) is shown in the card, and nothing is said saved",
  async (status, code, title) => {
    api({ "PUT /api/forwarding/settings": problem(status, code, title) });
    renderApp(PATH);
    const part = await forwarding();
    await userEvent.click(await within(part).findByRole("button", { name: m.forwardingSave }));
    expect(await within(part).findByText(title)).toBeInTheDocument();
    expect(statusOf(part)).toBeEmptyDOMElement();
    expect(within(part).getByRole("button", { name: m.forwardingSave })).toBeEnabled();
    // no field is marked
    expect(part.querySelector('[aria-invalid="true"]')).toBeNull();
  },
);

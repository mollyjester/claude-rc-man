import { PROTOCOL, readFrame, resizeFrame } from "./terminalSocket";

const sent = (cols: number, rows: number) => JSON.parse(resizeFrame({ cols, rows }));

test("a resize gives whole numbers within what rcman takes", () => {
  expect(sent(80, 24)).toEqual({ type: PROTOCOL.resize, cols: 80, rows: 24 });
  expect(sent(0, 70000)).toEqual({ type: PROTOCOL.resize, cols: 1, rows: 65535 });
  expect(sent(Number.NaN, 80.7)).toEqual({ type: PROTOCOL.resize, cols: 1, rows: 80 });
  expect(sent(PROTOCOL.size_max, PROTOCOL.size_min)).toEqual({
    type: PROTOCOL.resize,
    cols: PROTOCOL.size_max,
    rows: PROTOCOL.size_min,
  });
});

test("rcman's frames are read, and anything else is unknown", () => {
  expect(readFrame(JSON.stringify({ type: "exit", code: 3 }))).toEqual({ type: "exit", code: 3 });
  expect(readFrame(JSON.stringify({ type: "exit" }))).toEqual({ type: "exit", code: null });
  expect(readFrame(JSON.stringify({ type: "closed", reason: "x" }))).toEqual({
    type: "closed",
    reason: "x",
  });
  const problem = { code: "c", title: "t", status: 404 };
  expect(readFrame(JSON.stringify({ type: "problem", problem }))).toEqual({
    type: "problem",
    problem,
  });
  for (const text of ["{x", "null", "[1]", '{"type":"closed"}', '{"type":"paste"}'])
    expect(readFrame(text), text).toEqual({ type: "unknown" });
});

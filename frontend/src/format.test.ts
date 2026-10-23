import { formatBytes, formatCount, formatPercent, formatSeconds, utcMoment } from "./format";

const NBSP = "\u00a0";
// What a figure reads as, its non-breaking space written as a plain one.
const plain = (text: string) => text.replaceAll(NBSP, " ");

test("sizes are in decimal SI units, as Podman's are, rounded to three significant digits", () => {
  expect(plain(formatBytes(0))).toBe("0 B");
  expect(plain(formatBytes(26))).toBe("26 B");
  expect(plain(formatBytes(999))).toBe("999 B");
  expect(plain(formatBytes(1000))).toBe("1 kB");
  expect(plain(formatBytes(11_390))).toBe("11.4 kB");
  expect(plain(formatBytes(8_104_000))).toBe("8.1 MB");
  expect(plain(formatBytes(1_234_000_000))).toBe("1.23 GB");
  expect(plain(formatBytes(45_000_000_000))).toBe("45 GB");
  expect(plain(formatBytes(2_500_000_000_000))).toBe("2.5 TB");
});

test("a size that rounds up to the next unit is said in that unit", () => {
  expect(plain(formatBytes(999_950))).toBe("1 MB");
  expect(plain(formatBytes(999_499))).toBe("999 kB");
});

test("a percent, a count and an interval are numbers in the page's language", () => {
  expect(formatPercent(250.5)).toBe("250.5%");
  expect(formatPercent(0)).toBe("0%");
  expect(formatCount(41_234)).toBe("41,234");
  expect(plain(formatSeconds(15))).toBe("15 s");
  expect(plain(formatSeconds(90))).toBe("90 s");
  expect(plain(formatSeconds(60))).toBe("1 min");
  expect(plain(formatSeconds(5400))).toBe("90 min");
  expect(plain(formatSeconds(3600))).toBe("1 h");
  expect(plain(formatSeconds(86_400))).toBe("24 h");
});

test("a number and its unit are kept together by a non-breaking space", () => {
  for (const text of [formatBytes(1_234_000_000), formatSeconds(60)]) {
    expect(text).toContain(NBSP);
    expect(text).not.toContain(" ");
  }
});

test("a moment is a date and a time in UTC, as the audit trail shows one; what is not one is shown as it is", () => {
  expect(utcMoment("2026-10-23T10:00:00+00:00")).toBe("2026-10-23 10:00:00");
  expect(utcMoment("2026-10-23T12:00:00+02:00")).toBe("2026-10-23 10:00:00");
  expect(utcMoment("planted")).toBe("planted");
});

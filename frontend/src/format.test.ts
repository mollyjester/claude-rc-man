import { formatBytes, utcMoment } from "./format";

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

test("a moment is a date and a time in UTC, as the audit trail shows one; what is not one is shown as it is", () => {
  expect(utcMoment("2026-10-23T10:00:00+00:00")).toBe("2026-10-23 10:00:00");
  expect(utcMoment("2026-10-23T12:00:00+02:00")).toBe("2026-10-23 10:00:00");
  expect(utcMoment("planted")).toBe("planted");
});

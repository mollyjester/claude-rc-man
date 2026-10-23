import { m } from "./messages";

// Figures in the page's language, which is the message catalogue's (index.html's lang): the
// words around a number and its punctuation are of one language.
const LANGUAGE = "en";
// Between a number and its unit, so that a line never breaks between them.
const NBSP = "\u00a0";
// Decimal SI: a step of a thousand, as Podman gives sizes.
const SI_STEP = 1000;

const SIZE = new Intl.NumberFormat(LANGUAGE, { maximumSignificantDigits: 3 });
const PERCENT = new Intl.NumberFormat(LANGUAGE, { maximumFractionDigits: 1 });
const COUNT = new Intl.NumberFormat(LANGUAGE, { maximumFractionDigits: 0 });

// A size in bytes, in decimal SI units, as Podman's are; the page rounds to three significant
// digits, where Podman gives up to four.
export function formatBytes(bytes: number): string {
  let value = bytes;
  let unit = 0;
  // A value that rounds up to a thousand of its unit is said in the next.
  while (unit < m.byteUnits.length - 1 && Number(value.toPrecision(3)) >= SI_STEP) {
    value /= SI_STEP;
    unit += 1;
  }
  return `${SIZE.format(value)}${NBSP}${m.byteUnits[unit]}`;
}

// A percent as the server gives one, of one core for CPU, written the English way, with no
// space before the sign.
export const formatPercent = (percent: number) => `${PERCENT.format(percent)}%`;

export const formatCount = (count: number) => COUNT.format(count);

// A number of seconds in the largest unit that holds it whole.
export function formatSeconds(seconds: number): string {
  if (seconds > 0 && seconds % 3600 === 0)
    return `${COUNT.format(seconds / 3600)}${NBSP}${m.timeUnits.hour}`;
  if (seconds > 0 && seconds % 60 === 0)
    return `${COUNT.format(seconds / 60)}${NBSP}${m.timeUnits.minute}`;
  return `${COUNT.format(seconds)}${NBSP}${m.timeUnits.second}`;
}

// A moment as a date and a time in UTC, as the audit trail shows one, the same for everyone;
// what cannot be read as one is shown as it is.
export function utcMoment(iso: string): string {
  const at = new Date(iso);
  return Number.isNaN(at.getTime()) ? iso : at.toISOString().slice(0, 19).replace("T", " ");
}

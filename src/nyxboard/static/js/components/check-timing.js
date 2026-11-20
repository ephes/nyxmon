// JavaScript Date supports +/- 8.64e15 milliseconds from the Unix epoch.
const MAX_DATE_SECONDS = 8_640_000_000_000;

export function parseCheckTimestamp(value) {
  if (value === null || !/^-?\d+$/.test(value)) return null;

  const seconds = Number(value);
  return Math.abs(seconds) <= MAX_DATE_SECONDS ? seconds : null;
}

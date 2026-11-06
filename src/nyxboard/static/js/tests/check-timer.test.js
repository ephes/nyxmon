import { beforeEach, afterEach, it, expect, vi } from 'vitest';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { execFileSync } from 'node:child_process';
import '../components/check-timer.js';

function timer(next) {
  const element = document.createElement('check-timer');
  element.setAttribute('next-check', Math.floor(next.getTime() / 1000));
  document.body.append(element);
  return element;
}
beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  document.body.replaceChildren();
  vi.useRealTimers();
});
it('changes the heading and countdown together at the deadline', () => {
  vi.setSystemTime(new Date(2026, 0, 2, 12, 0, 0));
  const element = timer(new Date(2026, 0, 2, 12, 0, 2));
  expect(element.nextCheckTime.textContent).toMatch(/^Today at /);
  const text = element.nextCheckTime.firstChild;
  vi.advanceTimersByTime(1000);
  expect(element.nextCheckTime.firstChild).toBe(text);
  vi.advanceTimersByTime(1000);
  expect(element.nextCheckTime.textContent).toBe('Due now');
  expect(element.nextCheckCountdown.textContent).toBe('Due now');
});
it('reclassifies Tomorrow as Today at midnight without an attribute change', () => {
  vi.setSystemTime(new Date(2026, 0, 2, 23, 59, 59));
  const element = timer(new Date(2026, 0, 3, 12));
  expect(element.nextCheckTime.textContent).toMatch(/^Tomorrow at /);
  vi.advanceTimersByTime(1000);
  expect(element.nextCheckTime.textContent).toMatch(/^Today at /);
});
it.each([
  ['2026-03-08T16:00:00Z', '2026-03-09T16:00:00Z'],
  ['2026-11-01T17:00:00Z', '2026-11-02T17:00:00Z'],
])('uses calendar tomorrow across DST at %s', (now, next) => {
  // Pin the process timezone before Date initializes; independent of the host timezone.
  const source = `
    import { JSDOM } from 'jsdom';
    const dom = new JSDOM('');
    globalThis.HTMLElement = dom.window.HTMLElement;
    globalThis.customElements = dom.window.customElements;
    const RealDate = Date;
    globalThis.Date = class extends RealDate {
      constructor(...args) { super(...(args.length ? args : [${JSON.stringify(now)}])); }
      static now() { return new RealDate(${JSON.stringify(now)}).getTime(); }
    };
    await import(${JSON.stringify(pathToFileURL(resolve('src/nyxboard/static/js/components/check-timer.js')).href)});
    const element = dom.window.document.createElement('check-timer');
    console.log(element.formatNextCheckTime(new RealDate(${JSON.stringify(next)}).getTime() / 1000));
    dom.window.close();
  `;
  expect(execFileSync(process.execPath, ['--input-type=module', '-e', source], {
    encoding: 'utf8', env: { ...process.env, TZ: 'America/New_York' },
  }).trim()).toMatch(/^Tomorrow at /);
});

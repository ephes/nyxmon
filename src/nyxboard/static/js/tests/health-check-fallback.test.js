import { beforeEach, afterEach, it, expect, vi } from 'vitest';
import '../components/progress-ring.js';
import '../components/check-timer.js';
import '../components/health-check.js';

let element;
const statusUrl = () => new URL('/healthchecks/123/status/', window.location.href).href;
const fragment = (id = '123') => `\n <health-check id="check-${id}" check-id="${id}" status="failed" check-mode="normal"></health-check>\n`;
const response = (html = fragment(), overrides = {}) => ({
  ok: true, redirected: false, url: statusUrl(), text: vi.fn().mockResolvedValue(html), ...overrides,
});
const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
function due() {
  element.querySelector('progress-ring').dispatchEvent(new CustomEvent('check-due'));
}
beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(1800000000000);
  vi.stubGlobal('fetch', vi.fn());
  window.htmx = undefined;
  element = document.createElement('health-check');
  element.id = 'check-123';
  element.setAttribute('check-id', '123');
  element.setAttribute('check-mode', 'normal');
  element.innerHTML = '<progress-ring></progress-ring><check-timer></check-timer>';
  document.body.append(element);
});
afterEach(() => {
  document.body.replaceChildren();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});
it.each([
  ['500', () => response('<h1>Failure</h1>', { ok: false })],
  ['redirected login', () => response('<h1>Login</h1>', { redirected: true })],
  ['cross-origin', () => response(fragment(), { url: 'https://example.invalid/status/' })],
  ['wrong id', () => response(fragment('456'))],
  ['wrong check-id', () => response(fragment().replace('check-id="123"', 'check-id="456"'))],
  ['wrong element', () => response('<div id="check-123" check-id="123"></div>')],
  ['extra element', () => response(fragment() + '<div>unexpected</div>')],
])('retains the old check for %s and offers a delayed retry', async (_, makeResponse) => {
  fetch.mockResolvedValue(makeResponse());
  due();
  await settle();
  expect(document.getElementById('check-123')).toBe(element);
  expect(element._htmxCallInProgress).toBe(false);
  const retry = element.querySelector('[data-refresh-error] button');
  expect(retry).not.toBeNull();
  expect(retry.disabled).toBe(true);
  retry.click();
  expect(fetch).toHaveBeenCalledTimes(1);
  vi.advanceTimersByTime(5000);
  expect(retry.disabled).toBe(false);
  fetch.mockResolvedValue(response());
  retry.click();
  await settle();
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(document.getElementById('check-123')).not.toBe(element);
});
it('accepts whitespace-prefixed valid fragments and releases deadline state', async () => {
  fetch.mockResolvedValue(response());
  due();
  const request = element._refreshRequest;
  await settle();
  expect(document.getElementById('check-123').getAttribute('status')).toBe('failed');
  expect(element.isConnected).toBe(false);
  expect(element._retryOnConnect).toBe(false);
  expect(element._refreshRequest).toBeNull();
  expect(element._htmxCallInProgress).toBe(false);
  expect(vi.getTimerCount()).toBe(0);
  expect(request.controller.signal.aborted).toBe(true); // replacement disconnects owner
});
it('times out a never-resolving fetch and permits one bounded retry', async () => {
  fetch.mockImplementation(() => new Promise(() => {}));
  due();
  due();
  const signal = fetch.mock.calls[0][1].signal;
  expect(fetch).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(10000);
  expect(signal.aborted).toBe(true);
  expect(element._refreshRequest).toBeNull();
  expect(element._htmxCallInProgress).toBe(false);
  expect(element.querySelector('[data-refresh-error]')).not.toBeNull();
  const retry = element.querySelector('[data-refresh-error] button');
  expect(retry.disabled).toBe(true);
  await vi.advanceTimersByTimeAsync(5000);
  retry.click();
  retry.click();
  expect(fetch).toHaveBeenCalledTimes(2);
});
it('bounds reading the response text as well as acquiring headers', async () => {
  fetch.mockResolvedValue(response('', { text: () => new Promise(() => {}) }));
  due();
  await settle();
  await vi.advanceTimersByTimeAsync(10000);
  expect(element._htmxCallInProgress).toBe(false);
  expect(document.getElementById('check-123')).toBe(element);
  expect(element.querySelector('[data-refresh-error]')).not.toBeNull();
});
it('aborts on detach and rejects a late prior response after reconnect', async () => {
  let finishOld;
  fetch.mockImplementationOnce(() => new Promise(resolve => { finishOld = resolve; }))
    .mockImplementationOnce(() => new Promise(() => {}));
  due();
  const firstSignal = fetch.mock.calls[0][1].signal;
  element.remove();
  expect(firstSignal.aborted).toBe(true);
  expect(element._refreshRequest).toBeNull();
  document.body.append(element);
  expect(fetch).toHaveBeenCalledTimes(2);
  finishOld(response());
  await settle();
  expect(document.getElementById('check-123')).toBe(element);
  expect(element._refreshRequest).not.toBeNull();
  expect(element._htmxCallInProgress).toBe(true);
});
it('restarts after an error followed by disconnect and reconnect', async () => {
  fetch.mockResolvedValueOnce(response('', { ok: false }))
    .mockImplementationOnce(() => new Promise(() => {}));
  due();
  await settle();
  expect(element.querySelector('[data-refresh-error]')).not.toBeNull();
  element.remove();
  document.body.append(element);
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(element._htmxCallInProgress).toBe(true);
});
it('restarts a cancelled HTMX delay once on reconnect', () => {
  window.htmx = { ajax: vi.fn() };
  due();
  element.remove();
  vi.advanceTimersByTime(100);
  expect(window.htmx.ajax).not.toHaveBeenCalled();
  document.body.append(element);
  vi.advanceTimersByTime(50);
  expect(window.htmx.ajax).toHaveBeenCalledTimes(1);
});

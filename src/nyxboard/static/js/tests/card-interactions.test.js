import { it, expect, afterEach, vi } from 'vitest';
import '../dashboard.js';
import { fixtures } from '../../../../../tests/browser/fixture-server.js';
function event(name, elt, detail) {
  return elt.dispatchEvent(new CustomEvent(name, { bubbles: true, cancelable: true, detail: { elt, ...detail } }));
}
function render() {
  document.body.innerHTML = fixtures.fragments['1-False'] + fixtures.fragments['2-False'];
  return document.querySelector('#check-1 input[type=checkbox]');
}
afterEach(() => { document.body.replaceChildren(); vi.restoreAllMocks(); vi.clearAllMocks(); });

it('owns one pending toggle and restores the confirmed value after rejection', () => {
  const checkbox = render(); const xhr = { status: 403 };
  checkbox.checked = true;
  event('htmx:beforeRequest', checkbox, { xhr, requestConfig: { headers: {} } });
  expect(checkbox.disabled).toBe(true);
  expect(document.querySelector('#check-2 input').disabled).toBe(false);
  expect(event('htmx:beforeRequest', checkbox, { xhr: {}, requestConfig: {} })).toBe(false);
  expect(event('htmx:beforeRequest', document.body, { xhr: {}, target: document.getElementById('check-1'), requestConfig: {} })).toBe(false);
  event('htmx:afterRequest', checkbox, { xhr, successful: false });
  expect(checkbox.disabled).toBe(false); expect(checkbox.checked).toBe(false);
  expect(document.querySelector('#check-1 .toggle-label').textContent).toBe('Enabled');
  expect(document.querySelector('#check-1 [role=alert]').textContent).toContain('rejected');
});

it('reconciles an ambiguous toggle with GET instead of another inversion', async () => {
  const checkbox = render(); const xhr = { status: 0 };
  const ajax = vi.spyOn(window.htmx, 'ajax').mockImplementation(async (_method, _url, options) => {
    const card = document.getElementById('check-1'); const xhr = {};
    event('htmx:beforeRequest', card, { xhr, requestConfig: { headers: options.headers } });
    event('htmx:beforeSwap', card, { xhr, target: card, shouldSwap: true, serverResponse: fixtures.fragments['1-True'] });
    event('htmx:beforeCleanupElement', card, {});
    card.outerHTML = fixtures.fragments['1-True'];
    event('htmx:afterSwap', document.getElementById('check-1'), { xhr });
  });
  checkbox.checked = true;
  event('htmx:beforeRequest', checkbox, { xhr, requestConfig: { headers: {} } });
  event('htmx:afterRequest', checkbox, { xhr, successful: false });
  await Promise.resolve(); await Promise.resolve();
  expect(ajax).toHaveBeenCalledTimes(1);
  expect(ajax.mock.calls[0][0]).toBe('GET');
  expect(ajax.mock.calls[0][1]).toBe('/healthchecks/1/status/');
  expect(document.querySelector('#check-1 input').checked).toBe(true);
  expect(document.querySelector('#check-1 input').disabled).toBe(false);
});

it('restores a replaced action and uses the card when the action disappears', () => {
  render();
  const action = document.querySelector('#check-1 button'); action.focus();
  expect(action.id).toBe('check-1-run');
  for (const disabled of [false, true]) {
    const card = document.getElementById('check-1'); const xhr = {};
    event('htmx:beforeSwap', card, { xhr, target: card });
    card.outerHTML = fixtures.fragments[disabled ? '1-True' : '1-False'];
    const fresh = document.getElementById('check-1');
    event('htmx:afterSwap', fresh, { xhr, target: fresh });
    expect(document.activeElement).toBe(disabled ? fresh : document.getElementById('check-1-run'));
  }
});

it('locks mutating run actions without discarding their response', () => {
  render(); const form = document.querySelector('#check-1 form'); const xhr = { status: 403 };
  event('htmx:beforeRequest', form, { xhr, requestConfig: { headers: {} } });
  expect(document.querySelector('#check-1 input').disabled).toBe(true);
  expect(document.querySelector('#check-1 button').disabled).toBe(true);
  event('htmx:afterRequest', form, { xhr, successful: false });
  expect(document.querySelector('#check-1 input').disabled).toBe(false);
  expect(document.querySelector('#check-1 input').checked).toBe(false);
});

it.each([
  '',
  '<html><body>Unexpected page</body></html>',
  fixtures.fragments['2-True'],
  fixtures.fragments['1-True'].replace('check-id="1"', 'check-id="2"'),
  fixtures.fragments['1-True'] + fixtures.fragments['2-True'],
  'Unexpected text' + fixtures.fragments['1-True'],
])('rejects an invalid card fragment before swapping', serverResponse => {
  render();
  const card = document.getElementById('check-1');
  const detail = { xhr: {}, elt: card, target: card, shouldSwap: true, serverResponse };
  card.dispatchEvent(new CustomEvent('htmx:beforeSwap', { bubbles: true, detail }));
  expect(detail.shouldSwap).toBe(false);
});

it('binds GET response identity to its request card even when a header retargets the swap', () => {
  render();
  const card = document.getElementById('check-1'); const other = document.getElementById('check-2'); const xhr = {};
  event('htmx:beforeRequest', card, { xhr, requestConfig: { headers: {} } });
  const detail = { xhr, elt: other, target: other, shouldSwap: true, serverResponse: fixtures.fragments['2-True'] };
  other.dispatchEvent(new CustomEvent('htmx:beforeSwap', { bubbles: true, detail }));
  expect(detail.shouldSwap).toBe(false);
});

it('replaces a stale-definition conflict with its current server card', () => {
  const checkbox = render();
  const xhr = { status: 409 };
  checkbox.checked = true;
  event('htmx:beforeRequest', checkbox, { xhr, requestConfig: { headers: {} } });
  const card = document.getElementById('check-1');
  const detail = { xhr, target: card, shouldSwap: false, isError: true, serverResponse: fixtures.fragments['1-True'] };
  const swap = new CustomEvent('htmx:beforeSwap', { bubbles: true, detail });
  card.dispatchEvent(swap);
  expect(detail.shouldSwap).toBe(true);
  expect(detail.isError).toBe(false);
  event('htmx:beforeCleanupElement', card, {});
  card.outerHTML = fixtures.fragments['1-True'];
  event('htmx:afterSwap', document.getElementById('check-1'), { xhr });
  event('htmx:afterRequest', document.getElementById('check-1'), { xhr, successful: false });
  expect(document.querySelector('#check-1 input').checked).toBe(true);
  expect(document.querySelector('#check-1 input').disabled).toBe(false);
});

it('reconciles a successful no-swap mutation and keeps controls locked if GET also has no swap', async () => {
  const checkbox = render(); const xhr = { status: 204 };
  const ajax = vi.spyOn(window.htmx, 'ajax').mockResolvedValue();
  checkbox.checked = true;
  event('htmx:beforeRequest', checkbox, { xhr, requestConfig: { headers: {} } });
  event('htmx:afterRequest', checkbox, { xhr, successful: true });
  await Promise.resolve(); await Promise.resolve();
  expect(ajax).toHaveBeenCalledTimes(1);
  expect(ajax.mock.calls[0][0]).toBe('GET');
  expect(checkbox.disabled).toBe(true); expect(checkbox.checked).toBe(false);
  expect(document.querySelector('#check-1 [role=alert]').textContent).toContain('uncertain');

});

it.each([0, 200, 403])('retires only a cleaned-up card owner and fences late %s callbacks', async status => {
  const html = fixtures.fragments['1-False'].replaceAll('check-1', 'check-77').replaceAll('check-id="1"', 'check-id="77"');
  document.body.innerHTML = html;
  const old = document.getElementById('check-77');
  const oldXhr = { status };
  event('htmx:beforeRequest', old.querySelector('input'), { xhr: oldXhr, requestConfig: {} });
  // A child's cleanup must not retire its containing card's reservation.
  event('htmx:beforeCleanupElement', old.querySelector('input'), {});
  expect(event('htmx:beforeRequest', old.querySelector('input'), { xhr: {}, requestConfig: {} })).toBe(false);
  event('htmx:beforeCleanupElement', old, {});
  old.outerHTML = html;
  const fresh = document.getElementById('check-77');
  const input = fresh.querySelector('input'); const currentXhr = { status: 403 };
  expect(event('htmx:beforeRequest', input, { xhr: currentXhr, requestConfig: {} })).toBe(true);
  const staleSwap = { xhr: oldXhr, target: fresh, shouldSwap: true, serverResponse: html };
  fresh.dispatchEvent(new CustomEvent('htmx:beforeSwap', { bubbles: true, detail: staleSwap }));
  expect(staleSwap.shouldSwap).toBe(false);
  const ajax = vi.spyOn(window.htmx, 'ajax').mockResolvedValue();
  event('htmx:afterRequest', document.body, { xhr: oldXhr, successful: status === 200 });
  await Promise.resolve(); await Promise.resolve();
  expect(ajax).not.toHaveBeenCalled();
  expect(input.disabled).toBe(true);
  expect(event('htmx:beforeRequest', input, { xhr: {}, requestConfig: {} })).toBe(false);
  event('htmx:afterRequest', input, { xhr: currentXhr, successful: false });
  expect(input.disabled).toBe(false);
  expect(event('htmx:beforeRequest', input, { xhr: currentXhr, requestConfig: {} })).toBe(true);
  event('htmx:afterRequest', input, { xhr: currentXhr, successful: false });
});

it('ignores a reconciliation continuation after cleanup and a newer reservation', async () => {
  const html = fixtures.fragments['1-False'].replaceAll('check-1', 'check-78').replaceAll('check-id="1"', 'check-id="78"');
  document.body.innerHTML = html;
  const old = document.getElementById('check-78'); const xhr = { status: 0 };
  let finish;
  const ajax = vi.spyOn(window.htmx, 'ajax').mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  event('htmx:beforeRequest', old.querySelector('input'), { xhr, requestConfig: {} });
  event('htmx:afterRequest', old, { xhr, successful: false });
  await Promise.resolve();
  expect(ajax).toHaveBeenCalledOnce();
  event('htmx:beforeCleanupElement', old, {});
  old.outerHTML = html;
  const input = document.querySelector('#check-78 input'); const current = { status: 403 };
  expect(event('htmx:beforeRequest', input, { xhr: current, requestConfig: {} })).toBe(true);
  finish(); await Promise.resolve(); await Promise.resolve();
  expect(input.disabled).toBe(true);
  expect(document.querySelector('#check-78 [data-toggle-feedback]').textContent).toBe('Saving check state…');
  expect(old.textContent).not.toContain('State is uncertain');
  expect(event('htmx:beforeRequest', input, { xhr: {}, requestConfig: {} })).toBe(false);
  event('htmx:afterRequest', input, { xhr: current, successful: false });
});

it('cannot confirm an unrelated later generation after a validated replacement', () => {
  const html = fixtures.fragments['1-False'].replaceAll('check-1', 'check-79').replaceAll('check-id="1"', 'check-id="79"');
  document.body.innerHTML = html;
  const old = document.getElementById('check-79'); const xhr = { status: 200 };
  event('htmx:beforeRequest', old.querySelector('input'), { xhr, requestConfig: {} });
  event('htmx:beforeSwap', old, { xhr, target: old, shouldSwap: true, serverResponse: html });
  event('htmx:beforeCleanupElement', old, {});
  old.outerHTML = html;
  const confirmed = document.getElementById('check-79');
  event('htmx:afterSwap', confirmed, { xhr });
  event('htmx:beforeCleanupElement', confirmed, {});
  confirmed.outerHTML = html;
  const newer = document.getElementById('check-79');
  newer.setAttribute('aria-busy', 'true');
  newer.querySelector('input').disabled = true;
  event('htmx:afterRequest', document.body, { xhr, successful: true });
  expect(newer.querySelector('input').disabled).toBe(true);
  expect(newer.getAttribute('aria-busy')).toBe('true');
});

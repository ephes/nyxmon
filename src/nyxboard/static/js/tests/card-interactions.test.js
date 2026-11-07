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
afterEach(() => { document.body.replaceChildren(); vi.restoreAllMocks(); });

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
  const ajax = vi.spyOn(window.htmx, 'ajax').mockImplementation(async () => {
    document.getElementById('check-1').outerHTML = fixtures.fragments['1-True'];
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

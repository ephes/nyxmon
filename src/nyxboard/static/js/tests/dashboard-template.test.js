import { it, expect, vi } from 'vitest';
import { JSDOM, VirtualConsole } from 'jsdom';
import { getByRole } from '@testing-library/dom';
import { fixtures } from '../../../../../tests/browser/fixture-server.js';

it('renders distinct control names in the production templates', () => {
  const dom = new JSDOM(fixtures.html);
  const body = dom.window.document.body;
  expect(getByRole(body, 'checkbox', { name: 'Dark theme', exact: true })).toBeTruthy();
  for (const name of ['Alpha', 'Beta']) {
    expect(getByRole(body, 'checkbox', { name: `Disable check ${name}`, exact: true }).checked).toBe(false);
    expect(getByRole(body, 'button', { name: `Run now: ${name}`, exact: true })).toBeTruthy();
  }
  dom.window.close();
});

it('executes production theme scripts despite storage and session rejection', async () => {
  const themes = [];
  const dom = new JSDOM(fixtures.html, {
    url: 'http://localhost/', runScripts: 'dangerously', virtualConsole: new VirtualConsole(),
    beforeParse(window) {
      window.matchMedia = () => ({ matches: true });
      window.fetch = vi.fn().mockRejectedValue(new Error('offline'));
      window.Storage.prototype.getItem = () => { throw new Error('denied'); };
      window.Storage.prototype.setItem = () => { throw new Error('denied'); };
      window.document.addEventListener('theme-changed', event => themes.push(event.detail.theme));
    },
  });
  await new Promise(resolve => dom.window.addEventListener('load', resolve));
  try {
    const toggle = dom.window.document.getElementById('themeToggle');
    expect(toggle.checked).toBe(true);
    for (const checked of [false, true]) {
      toggle.checked = checked;
      toggle.dispatchEvent(new dom.window.Event('change'));
      expect(themes.at(-1)).toBe(checked ? 'dark' : 'light');
      expect(dom.window.document.documentElement.dataset.theme).toBe(checked ? 'dark' : 'light');
    }
    await Promise.resolve();
  } finally { dom.window.close(); }
});

it('keeps the visible processing state in the accessible name', () => {
  const dom = new JSDOM(fixtures.fragments['1-processing']);
  expect(getByRole(dom.window.document.body, 'button', { name: 'Running... Alpha', exact: true })).toBeTruthy();
  dom.window.close();
});

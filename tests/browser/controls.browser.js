import { test, expect } from '@playwright/test';
import { serve, fixtures } from './fixture-server.js';
let server;
test.beforeAll(async () => { server = await serve(); });
test.afterAll(async () => { await server.close(); });

test('named controls distinguish checks and Space operates the intended checkbox', async ({ page }) => {
  await page.route('**/healthchecks/1/toggle-disabled/', route => route.fulfill({ contentType: 'text/html', body: fixtures.fragments['1-True'] }));
  await page.goto(server.url);
  const theme = page.getByRole('checkbox', { name: 'Dark theme', exact: true });
  await expect(theme).not.toBeChecked();
  await theme.focus();
  await page.keyboard.press('Space');
  await expect(theme).toBeChecked();
  const alpha = page.getByRole('checkbox', { name: 'Disable check Alpha', exact: true });
  const beta = page.getByRole('checkbox', { name: 'Disable check Beta', exact: true });
  await expect(alpha).not.toBeChecked();
  await expect(beta).not.toBeChecked();
  await expect(page.getByRole('button', { name: 'Run now: Alpha', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Run now: Beta', exact: true })).toBeVisible();
  await alpha.focus();
  await page.keyboard.press('Space');
  await expect(alpha).toBeChecked();
  await expect(beta).not.toBeChecked();
  await expect(page.locator('#check-1 .toggle-label')).toHaveText('Disabled');
});

test('storage denial and session failure preserve theme events and component state', async ({ page }) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.addInitScript(() => {
    Storage.prototype.getItem = () => { throw new Error('storage denied'); };
    Storage.prototype.setItem = () => { throw new Error('storage denied'); };
    window.themeEvents = [];
    document.addEventListener('theme-changed', event => window.themeEvents.push(event.detail.theme));
  });
  await page.route('**/set-theme/', route => route.abort());
  await page.goto(server.url);
  const theme = page.getByRole('checkbox', { name: 'Dark theme', exact: true });
  await expect(theme).toBeChecked();
  for (const dark of [false, true]) {
    await theme.focus(); await page.keyboard.press('Space');
    await expect(theme).toBeChecked({ checked: dark });
    await expect(page.locator('html')).toHaveAttribute('data-theme', dark ? 'dark' : 'light');
    await expect(page.locator('health-check').first()).toHaveClass(dark ? /dark-themed/ : /^(?!.*dark-themed)/);
    await expect.poll(() => page.evaluate(() => window.themeEvents.at(-1))).toBe(dark ? 'dark' : 'light');
  }
  expect(errors).toEqual([]);
});

for (const saved of ['light', 'dark', null]) {
  test(`saved ${saved} preference and system preference initialize consistently`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: 'dark' });
    await page.addInitScript(saved => { if (saved) localStorage.setItem('nyxmon-theme', saved); }, saved);
    await page.goto(server.url);
    await expect(page.getByRole('checkbox', { name: 'Dark theme', exact: true })).toBeChecked({ checked: saved !== 'light' });
  });
}

test('processing actions announce Running with check identity', async ({ page }) => {
  await page.route('**/healthchecks/1/trigger/', route => route.fulfill({ contentType: 'text/html', body: fixtures.fragments['1-processing'] }));
  await page.goto(server.url);
  await page.getByRole('button', { name: 'Run now: Alpha', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Running... Alpha', exact: true })).toBeVisible();
});

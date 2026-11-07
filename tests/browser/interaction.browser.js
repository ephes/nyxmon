import { test, expect } from '@playwright/test';
import { serve, fixtures } from './fixture-server.js';
let server;
test.beforeAll(async () => { server = await serve(); });
test.afterAll(async () => { await server.close(); });

for (const failure of [403, 500, 'disconnect']) {
  test(`toggle ${failure} reconciles without repeating the inversion POST`, async ({ page }) => {
    let posts = 0, gets = 0;
    await page.route('**/healthchecks/1/toggle-disabled/', async route => {
      posts++;
      if (failure === 'disconnect') await route.abort();
      else await route.fulfill({ status: failure, body: 'synthetic rejection' });
    });
    await page.route('**/healthchecks/1/status/', route => {
      gets++;
      return route.fulfill({ contentType: 'text/html', body: fixtures.fragments[failure === 403 ? '1-False' : '1-True'] });
    });
    await page.goto(server.url);
    const checkbox = page.getByRole('checkbox', { name: 'Disable check Alpha', exact: true });
    await checkbox.focus(); await page.keyboard.press('Space');
    await expect(checkbox).toBeEnabled();
    await expect(checkbox).toBeChecked({ checked: failure !== 403 });
    await expect(page.locator('#check-1 .toggle-label')).toHaveText(failure === 403 ? 'Enabled' : 'Disabled');
    await expect(page.locator('#check-1 [data-toggle-feedback]')).toContainText(/failed|confirmed|rejected/i);
    expect(posts).toBe(1);
    expect(gets).toBe(failure === 403 ? 0 : 1);
  });
}

test('pending toggle is scoped and a successful retry recovers', async ({ page }) => {
  let release, posts = 0, gets = 0;
  await page.route('**/healthchecks/1/status/', route => { gets++; return route.fulfill({ contentType: 'text/html', body: fixtures.fragments['1-False'] }); });
  const gate = new Promise(resolve => { release = resolve; });
  await page.route('**/healthchecks/1/toggle-disabled/', async route => {
    posts++; await gate;
    await route.fulfill({ contentType: 'text/html', body: fixtures.fragments['1-True'] });
  });
  await page.goto(server.url);
  const checkbox = page.getByRole('checkbox', { name: 'Disable check Alpha', exact: true });
  await checkbox.focus(); await page.keyboard.press('Space');
  await expect(checkbox).toBeDisabled();
  await page.keyboard.press('Space');
  await page.evaluate(() => { window.htmx.ajax('GET', '/healthchecks/1/status/', { target: '#check-1', swap: 'outerHTML' }).catch(() => {}); });
  await expect(checkbox).toBeDisabled();
  await expect(page.getByRole('checkbox', { name: 'Disable check Beta', exact: true })).toBeEnabled();
  release();
  await expect(checkbox).toBeEnabled(); await expect(checkbox).toBeChecked();
  expect(posts).toBe(1); expect(gets).toBe(0);
});

for (const source of ['action', 'poll']) {
  test(`${source} card swaps preserve focus and fallback without stealing it`, async ({ page }) => {
    await page.route('**/healthchecks/1/trigger/', route => route.fulfill({ contentType: 'text/html', body: fixtures.fragments['1-False'] }));
    let disabled = false;
    await page.route('**/healthchecks/1/status/', route => route.fulfill({ contentType: 'text/html', body: fixtures.fragments[disabled ? '1-True' : '1-False'] }));
    await page.goto(server.url);
    const run = page.getByRole('button', { name: 'Run now: Alpha', exact: true });
    await run.focus();
    const swap = async () => {
      if (source === 'action' && !disabled) await run.press('Enter');
      else await page.evaluate(() => window.htmx.ajax('GET', '/healthchecks/1/status/', { source: document.getElementById('check-1'), target: '#check-1', swap: 'outerHTML' }));
    };
    await swap(); await expect(run).toBeFocused();
    await page.keyboard.press('Tab');
    expect(await page.evaluate(() => document.activeElement.tagName)).not.toBe('BODY');
    await run.focus(); disabled = true;
    await swap(); await expect(page.locator('#check-1')).toBeFocused();
    const beta = page.getByRole('button', { name: 'Run now: Beta', exact: true });
    await beta.focus(); await swap(); await expect(beta).toBeFocused();
    await page.evaluate(() => {
      const input = document.createElement('input'); input.id = 'outside-card';
      document.body.append(input); input.value = 'synthetic selection'; input.focus();
      input.setSelectionRange(2, 8);
    });
    await swap(); await expect(page.locator('#outside-card')).toBeFocused();
    expect(await page.locator('#outside-card').evaluate(input => [input.selectionStart, input.selectionEnd])).toEqual([2, 8]);
  });
}

test('real check and mirrored service transitions announce once and ignore repeats/timers', async ({ page }) => {
  await page.goto(server.url);
  const region = page.getByRole('status', { name: 'Monitoring updates', exact: true });
  await page.evaluate(() => {
    const region = document.getElementById('monitoring-status');
    window.announcements = [];
    new MutationObserver(() => window.announcements.push(region.textContent)).observe(region, { childList: true, characterData: true, subtree: true });
    document.querySelectorAll('health-check').forEach(check => check.setAttribute('status', 'passed'));
  });
  // Settle the synthetic baseline transition, then measure failure/recovery.
  await expect(region).toContainText('Passed');
  await page.evaluate(() => { window.announcements = []; document.getElementById('check-1').setAttribute('status', 'failed'); });
  await expect(region).toContainText('Synthetic service: Failed');
  await page.evaluate(() => {
    document.getElementById('check-1').setAttribute('status', 'failed');
    document.getElementById('check-1').setAttribute('next-check', '4102444801');
  });
  await page.evaluate(() => document.getElementById('check-1').setAttribute('status', 'passed'));
  await expect(region).toContainText('Synthetic service: Passed');
  const messages = await page.evaluate(() => window.announcements);
  expect(messages).toHaveLength(2);
  for (const message of messages) expect(message.match(/Synthetic service:/g)).toHaveLength(1);
});

test('a definite rejection releases controls for a deliberate retry', async ({ page }) => {
  let posts = 0;
  await page.route('**/healthchecks/1/toggle-disabled/', route => {
    posts++;
    return route.fulfill(posts === 1 ? { status: 403, body: 'rejected' } : { contentType: 'text/html', body: fixtures.fragments['1-True'] });
  });
  await page.goto(server.url);
  const checkbox = page.getByRole('checkbox', { name: 'Disable check Alpha', exact: true });
  await checkbox.focus(); await page.keyboard.press('Space');
  await expect(checkbox).toBeEnabled(); await expect(checkbox).not.toBeChecked();
  await checkbox.focus(); await page.keyboard.press('Space');
  await expect(checkbox).toBeEnabled(); await expect(checkbox).toBeChecked();
  expect(posts).toBe(2);
});

test('failed reconciliation offers GET retry while inversion stays disabled', async ({ page }) => {
  let posts = 0, gets = 0;
  await page.route('**/healthchecks/1/toggle-disabled/', route => { posts++; return route.abort(); });
  await page.route('**/healthchecks/1/status/', route => {
    gets++;
    return route.fulfill(gets <= 2 ? { status: 503, body: 'unavailable' } : { contentType: 'text/html', body: fixtures.fragments['1-True'] });
  });
  await page.goto(server.url);
  const checkbox = page.getByRole('checkbox', { name: 'Disable check Alpha', exact: true });
  await checkbox.focus(); await page.keyboard.press('Space');
  const retry = page.getByRole('button', { name: 'Refresh status', exact: true });
  await expect(retry).toBeEnabled(); await expect(retry).toBeFocused();
  await expect(checkbox).toBeDisabled();
  await page.evaluate(() => { window.htmx.ajax('GET', '/healthchecks/1/status/', { target: '#check-1', swap: 'outerHTML' }).catch(() => {}); });
  await expect(retry).toBeVisible(); expect(gets).toBe(1);
  await retry.press('Enter');
  await expect(retry).toBeEnabled(); await expect(retry).toBeFocused();
  await expect.poll(() => gets).toBe(2);
  await retry.press('Enter');
  await expect(checkbox).toBeEnabled(); await expect(checkbox).toBeChecked();
  expect(posts).toBe(1); expect(gets).toBe(3);
});

test('a toggle supersedes an older poll so its late response cannot undo server state', async ({ page }) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  let release, seen;
  const gate = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { seen = resolve; });
  await page.route('**/healthchecks/1/status/', async route => {
    seen(); await gate;
    try { await route.fulfill({ contentType: 'text/html', body: fixtures.fragments['1-False'] }); } catch { /* Client cancelled the old poll. */ }
  });
  await page.route('**/healthchecks/1/toggle-disabled/', route => route.fulfill({ contentType: 'text/html', body: fixtures.fragments['1-True'] }));
  await page.goto(server.url);
  await page.evaluate(() => document.querySelector('#check-1 progress-ring').dispatchEvent(new CustomEvent('check-due', { bubbles: true })));
  await started;
  const checkbox = page.getByRole('checkbox', { name: 'Disable check Alpha', exact: true });
  await checkbox.focus(); await page.keyboard.press('Space');
  await expect(checkbox).toBeEnabled(); await expect(checkbox).toBeChecked();
  release();
  await expect(page.locator('#check-1 .toggle-label')).toHaveText('Disabled');
  await expect(checkbox).toBeChecked();
  expect(errors).toEqual([]);
});

test('Run now supersedes a poll and owns controls until its POST response arrives', async ({ page }) => {
  let releasePoll, releaseRun, pollSeen, runSeen, toggles = 0, polls = 0;
  const pollGate = new Promise(resolve => { releasePoll = resolve; });
  const runGate = new Promise(resolve => { releaseRun = resolve; });
  const polling = new Promise(resolve => { pollSeen = resolve; });
  const running = new Promise(resolve => { runSeen = resolve; });
  await page.route('**/healthchecks/1/status/', async route => {
    polls++;
    if (polls === 1) {
      pollSeen(); await pollGate;
      try { await route.fulfill({ contentType: 'text/html', body: fixtures.fragments['1-False'] }); } catch { /* Superseded poll. */ }
    } else {
      await route.fulfill({ contentType: 'text/html', body: fixtures.fragments['1-processing'] });
    }
  });
  await page.route('**/healthchecks/1/trigger/', async route => {
    runSeen(); await runGate;
    await route.fulfill({ contentType: 'text/html', body: fixtures.fragments['1-processing'] });
  });
  await page.route('**/healthchecks/1/toggle-disabled/', route => { toggles++; return route.abort(); });
  await page.goto(server.url);
  await page.evaluate(() => document.querySelector('#check-1 progress-ring').dispatchEvent(new CustomEvent('check-due', { bubbles: true })));
  await polling;
  await page.getByRole('button', { name: 'Run now: Alpha', exact: true }).press('Enter');
  await running;
  const toggle = page.getByRole('checkbox', { name: 'Disable check Alpha', exact: true });
  await expect(toggle).toBeDisabled();
  await toggle.evaluate(control => control.click());
  releaseRun(); releasePoll();
  await expect(page.getByRole('button', { name: 'Running... Alpha', exact: true })).toBeVisible();
  await expect(toggle).toBeEnabled();
  expect(toggles).toBe(0);
});

test('rejecting a toggle resumes the due poll that it superseded', async ({ page }) => {
  let seen, release, gets = 0;
  const first = new Promise(resolve => { seen = resolve; });
  const gate = new Promise(resolve => { release = resolve; });
  await page.route('**/healthchecks/1/status/', async route => {
    gets++;
    if (gets === 1) { seen(); await gate; }
    try { await route.fulfill({ contentType: 'text/html', body: fixtures.fragments['1-False'] }); } catch { /* Superseded poll. */ }
  });
  await page.route('**/healthchecks/1/toggle-disabled/', route => route.fulfill({ status: 403, body: 'synthetic rejection' }));
  await page.goto(server.url);
  await page.evaluate(() => document.querySelector('#check-1 progress-ring').dispatchEvent(new CustomEvent('check-due', { bubbles: true })));
  await first;
  const checkbox = page.getByRole('checkbox', { name: 'Disable check Alpha', exact: true });
  await checkbox.focus(); await page.keyboard.press('Space');
  await expect(checkbox).toBeEnabled(); await expect(checkbox).not.toBeChecked();
  release();
  await expect.poll(() => gets).toBe(2);
  await expect(page.locator('#check-1')).toHaveAttribute('check-mode', 'normal');
});

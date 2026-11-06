import { beforeEach, afterEach, it, expect, vi } from 'vitest';
import '../components/service-card.js';
import '../components/health-check.js';

function child(status) {
  const check = document.createElement('health-check');
  check.setAttribute('status', status);
  return check;
}
function service() {
  const card = document.createElement('service-card');
  card.innerHTML = '<div class="service-header"><span class="service-status status-passed"></span></div>';
  return card;
}
beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  document.body.replaceChildren();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

it('keeps one listener connected and removes it when detached', () => {
  const card = service();
  const update = vi.spyOn(card, 'updateServiceStatus');
  for (let cycle = 0; cycle < 3; cycle++) {
    document.body.append(card);
    update.mockClear();
    card.dispatchEvent(new CustomEvent('health-check-status-changed'));
    expect(update).toHaveBeenCalledTimes(1);
    card.remove();
    update.mockClear();
    card.dispatchEvent(new CustomEvent('health-check-status-changed'));
    expect(update).not.toHaveBeenCalled();
  }
});

it('recomputes from current siblings on replacement, status changes and recovery', () => {
  const card = service();
  card.setAttribute('service-id', '17');
  const overview = service();
  overview.setAttribute('service-id', '17');
  document.body.append(overview);
  const original = child('passed');
  const sibling = child('passed');
  card.append(original, sibling);
  document.body.append(card);
  const replacement = child('failed');
  original.replaceWith(replacement);
  expect(card.getAttribute('service-status')).toBe('failed');
  expect(card.querySelector('.service-header').classList.contains('failed')).toBe(true);
  expect(card.querySelector('.service-status').textContent).toBe('Failed');
  for (const element of [card, overview]) {
    expect(element.getAttribute('service-status')).toBe('failed');
    expect(element.classList.contains('failed')).toBe(true);
    expect(element.querySelector('.service-status').classList.contains('status-failed')).toBe(true);
    expect(element.querySelector('.service-status').classList.contains('status-passed')).toBe(false);
  }
  sibling.setAttribute('status', 'warning');
  replacement.setAttribute('status', 'passed');
  expect(card.getAttribute('service-status')).toBe('warning');
  sibling.setAttribute('status', 'passed');
  expect(card.getAttribute('service-status')).toBe('passed');
  const changed = vi.fn();
  original.addEventListener('health-check-status-changed', changed);
  original.setAttribute('status', 'failed');
  expect(changed).not.toHaveBeenCalled();
  expect(card.getAttribute('service-status')).toBe('passed');
});

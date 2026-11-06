import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest';
import '../components/progress-ring.js';
import '../components/check-timer.js';
import '../components/health-check.js';
import '../dashboard.js';

function check(id = '123', status = 'passed') {
  const element = document.createElement('health-check');
  element.id = `check-${id}`;
  element.setAttribute('check-id', id);
  element.setAttribute('status', status);
  element.setAttribute('check-mode', 'normal');
  element.setAttribute('next-check', 1800000060);
  element.setAttribute('check-interval', 60);
  element.innerHTML = '<progress-ring></progress-ring><check-timer></check-timer>';
  return element;
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(1800000000000);
  window.htmx = { ajax: vi.fn() };
  document.dispatchEvent(new Event('DOMContentLoaded'));
});
afterEach(() => {
  document.body.replaceChildren();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('production health check', () => {
  it('initializes real child components and propagates observed attributes', () => {
    const element = check();
    document.body.append(element);
    const ring = element.querySelector('progress-ring');
    const timer = element.querySelector('check-timer');
    expect(ring).toBeInstanceOf(customElements.get('progress-ring'));
    expect(timer).toBeInstanceOf(customElements.get('check-timer'));
    expect(ring.getAttribute('next-check')).toBe('1800000060');
    expect(ring.getAttribute('check-interval')).toBe('60');
    expect(timer.getAttribute('status')).toBe('passed');
    element.setAttribute('status', 'failed');
    element.setAttribute('next-check', '1800000120');
    element.setAttribute('check-interval', '120');
    element.setAttribute('check-mode', 'due');
    expect(timer.getAttribute('status')).toBe('failed');
    for (const child of [ring, timer]) {
      expect(child.getAttribute('mode')).toBe('due');
      expect(child.getAttribute('next-check')).toBe('1800000120');
    }
    expect(ring.getAttribute('check-interval')).toBe('120');
    expect(ring.progressText.textContent).toBe('100%');
  });

  it('dispatches once for the owning check despite duplicate due events', () => {
    const first = check('123');
    const second = check('456');
    document.body.append(first, second);
    const ring = second.querySelector('progress-ring');
    ring.dispatchEvent(new CustomEvent('check-due', { bubbles: true }));
    ring.dispatchEvent(new CustomEvent('check-due', { bubbles: true }));
    vi.advanceTimersByTime(50);
    expect(second.getAttribute('check-mode')).toBe('due');
    expect(first.getAttribute('check-mode')).toBe('normal');
    expect(window.htmx.ajax).toHaveBeenCalledExactlyOnceWith('GET', '/healthchecks/456/status/', {
      target: '#check-456', swap: 'outerHTML',
    });
  });

  it('ignores a due event from a detached check', () => {
    const element = check();
    document.body.append(element);
    element.remove();
    element.querySelector('progress-ring').dispatchEvent(new CustomEvent('check-due'));
    vi.advanceTimersByTime(100);
    expect(window.htmx.ajax).not.toHaveBeenCalled();
  });
});

it('keeps processing state separate from displayed health and ignores disabled checks', () => {
  const element = check();
  element.setAttribute('execution-status', 'processing');
  element.setAttribute('disabled', '');
  document.body.append(element);
  expect(element.querySelector('check-timer').getAttribute('status')).toBe('processing');
  element.querySelector('progress-ring').dispatchEvent(new CustomEvent('check-due'));
  vi.advanceTimersByTime(100);
  expect(window.htmx.ajax).not.toHaveBeenCalled();
});

import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest';
import '../components/progress-ring.js';

const now = 1800000000;
function ring(next = now + 60, interval = 60) {
  const element = document.createElement('progress-ring');
  element.setAttribute('next-check', next);
  element.setAttribute('check-interval', interval);
  document.body.append(element);
  return element;
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(now * 1000);
});
afterEach(() => {
  document.body.replaceChildren();
  vi.useRealTimers();
});

describe('production progress ring arithmetic', () => {
  it.each([
    ['before start', now + 90, 0],
    ['at start', now + 60, 0],
    ['halfway', now + 30, 50],
    ['exactly due', now, 100],
    ['overdue', now - 30, 100],
  ])('%s', (_, next, percentage) => {
    const element = ring(next);
    expect(element.calculateProgress(next, 60)).toBe(percentage);
    expect(element.shadowRoot.querySelector('.progress-percentage').textContent).toBe(`${percentage}%`);
    expect(Number(element.progressRing.style.strokeDashoffset)).toBeCloseTo(
      2 * Math.PI * 26 * (1 - percentage / 100),
    );
  });

  it('updates from changed timing attributes and advancing time', () => {
    const element = ring();
    vi.advanceTimersByTime(30000);
    expect(element.progressText.textContent).toBe('50%');
    element.setAttribute('next-check', now + 90);
    expect(element.progressText.textContent).toBe('0%');
    element.setAttribute('check-interval', 120);
    expect(element.progressText.textContent).toBe('50%');
  });

  it('renders rounded percentages and explicit due mode', () => {
    const element = ring(now + 40);
    expect(element.progressText.textContent).toBe('33%');
    element.setAttribute('mode', 'due');
    expect(element.progressText.textContent).toBe('100%');
  });
});

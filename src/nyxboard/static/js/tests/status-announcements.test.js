import { it, expect, afterEach } from 'vitest';
import '../components/service-card.js';
import '../components/health-check.js';
const settle = () => new Promise(resolve => setTimeout(resolve, 0));
afterEach(() => document.body.replaceChildren());

it('announces real component transitions once across mirrors and replacement', async () => {
  document.body.innerHTML = `
    <div id="monitoring-status" role="status" aria-label="Monitoring updates"></div>
    <service-card service-id="71" service-status="passed"><span class="service-name">Synthetic service</span><span class="service-status"></span></service-card>
    <service-card service-id="71" service-status="passed"><span class="service-name">Synthetic service</span><span class="service-status"></span>
      <health-check id="check-91" check-id="91" status="passed"><h3 class="health-check-title">Alpha</h3></health-check>
    </service-card>`;
  const region = document.getElementById('monitoring-status');
  await settle();
  expect(region.textContent).toBe('');
  const messages = [];
  const observer = new MutationObserver(() => messages.push(region.textContent));
  observer.observe(region, { childList: true });
  let check = document.getElementById('check-91');
  check.setAttribute('status', 'failed'); await settle();
  expect(region.textContent).toContain('Alpha: Failed.');
  expect(region.textContent.match(/Synthetic service: Failed\./g)).toHaveLength(1);
  check.setAttribute('status', 'failed');
  check.setAttribute('next-check', '4102444800'); await settle();
  const copy = check.cloneNode(true);
  check.replaceWith(copy); check = copy; await settle();
  expect(messages).toHaveLength(1);
  check.setAttribute('status', 'passed'); await settle();
  expect(region.textContent).toContain('Alpha: Passed.');
  expect(region.textContent.match(/Synthetic service: Passed\./g)).toHaveLength(1);
  expect(messages).toHaveLength(2);
  observer.disconnect();
});

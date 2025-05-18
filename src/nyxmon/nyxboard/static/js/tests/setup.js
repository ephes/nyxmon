/**
 * Setup file for Vitest tests
 * This file is loaded before tests are run
 */

// Polyfill for custom elements if running in a test environment
if (!window.customElements) {
  window.customElements = {
    define: vi.fn(),
  };
}

// Mock HTMX for testing
window.htmx = {
  ajax: vi.fn(),
  process: vi.fn(),
};
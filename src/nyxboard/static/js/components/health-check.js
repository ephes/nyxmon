import { announceStatus } from '../status-announcer.js';

/**
 * Custom element for displaying a health check with its status, progress, and controls.
 * This component integrates with HTMX for server interactions.
 */
class HealthCheck extends HTMLElement {
  constructor() {
    super();
    
    // We're using a regular DOM element (not Shadow DOM) to allow for HTMX integration
    this.checkDueHandler = this.handleCheckDue.bind(this);
    
    // Handler for theme changes
    this.themeChangeHandler = this.handleThemeChange.bind(this);
    
    // Debug flag
    this.debug = false;
  }
  
  static get observedAttributes() {
    return ['check-id', 'check-mode', 'next-check', 'check-interval', 'status', 'execution-status', 'theme'];
  }
  
  connectedCallback() {
    if (this.debug) console.log(`HealthCheck connected: #check-${this.getAttribute('check-id')}`);
    
    // Listen for the check-due event from the progress ring
    // We need to add this on the specific progress-ring element, not the parent
    const progressRing = this.querySelector('progress-ring');
    if (progressRing) {
      progressRing.addEventListener('check-due', this.checkDueHandler);
      if (this.debug) console.log('Added check-due event listener to progress-ring');
    }
    
    // Check for theme
    this.applyCurrentTheme();
    
    // Listen to theme changes on the document
    document.addEventListener('theme-changed', this.themeChangeHandler);
    
    // Initialize child components if needed
    this.updateComponentStatuses();
    this.notifyStatusChanged();
    if (this._retryOnConnect) {
      this._retryOnConnect = false;
      this.setAttribute('check-mode', 'normal');
      this.handleCheckDue();
    }
  }
  
  disconnectedCallback() {
    if (this.debug) console.log(`HealthCheck disconnected: #check-${this.getAttribute('check-id')}`);
    
    this._retryOnConnect = Boolean(this._refreshRequest || this._htmxTimer || this.querySelector('[data-refresh-error]'));
    this.cancelRefresh();
    // Clean up event listeners
    const progressRing = this.querySelector('progress-ring');
    if (progressRing) {
      progressRing.removeEventListener('check-due', this.checkDueHandler);
      if (this.debug) console.log('Removed check-due event listener from progress-ring');
    }
    
    // Remove theme change listener
    document.removeEventListener('theme-changed', this.themeChangeHandler);
  }
  
  attributeChangedCallback(name, oldValue, newValue) {
    if (oldValue === newValue) return;
    
    if (this.debug) console.log(`HealthCheck attribute changed: ${name} = ${newValue}`);
    
    // When attributes change, update the corresponding components
    if (name === 'check-mode' || name === 'status' || name === 'next-check' || name === 'check-interval' || name === 'execution-status') {
      this.updateComponentStatuses();
      if (name === 'status') this.notifyStatusChanged();
    } else if (name === 'theme') {
      this.applyCurrentTheme();
    }
  }
  
  notifyStatusChanged() {
    if (this.isConnected) {
      announceStatus('check', this.getAttribute('check-id'),
        this.querySelector('.health-check-title, .health-check-link')?.textContent.trim(),
        this.getAttribute('status'));
      this.dispatchEvent(new CustomEvent('health-check-status-changed', { bubbles: true }));
    }
  }

  updateComponentStatuses() {
    const mode = this.getAttribute('check-mode');
    const status = this.getAttribute('status');
    const nextCheck = this.getAttribute('next-check');
    const checkInterval = this.getAttribute('check-interval');
    
    // Update progress ring
    const progressRing = this.querySelector('progress-ring');
    if (progressRing) {
      if (mode) progressRing.setAttribute('mode', mode);
      if (nextCheck) progressRing.setAttribute('next-check', nextCheck);
      if (checkInterval) progressRing.setAttribute('check-interval', checkInterval);
    }
    
    // Update check timer
    const checkTimer = this.querySelector('check-timer');
    if (checkTimer) {
      if (mode) checkTimer.setAttribute('mode', mode);
      if (status) checkTimer.setAttribute('status', this.getAttribute('execution-status') === 'processing' ? 'processing' : status);
      if (nextCheck) checkTimer.setAttribute('next-check', nextCheck);
    }
  }
  
  handleCheckDue() {
    const checkId = this.getAttribute('check-id');
    if (!this.isConnected || this.hasAttribute('disabled') ||
        this.getAttribute('check-mode') === 'due' || this._htmxCallInProgress) return;
    this.setAttribute('check-mode', 'due');
    this._htmxCallInProgress = true;
    if (window.htmx) {
      this._htmxTimer = setTimeout(() => {
        this._htmxTimer = null;
        try {
          if (this.isConnected) {
            window.htmx.ajax('GET', `/healthchecks/${checkId}/status/`, {
              source: this, target: `#check-${checkId}`, swap: 'outerHTML'
            })?.catch(() => { /* A deliberate toggle may supersede this poll. */ });
          }
        } finally {
          this._htmxCallInProgress = false;
        }
      }, 50);
    } else {
      this.refreshStatus(checkId);
    }
  }

  async refreshStatus(checkId) {
    const controller = new AbortController();
    const request = { controller, timer: null };
    this._refreshRequest = request;
    const url = new URL(`/healthchecks/${checkId}/status/`, window.location.href);
    const aborted = new Promise((_, reject) => {
      controller.signal.addEventListener('abort', () => reject(new Error('Refresh cancelled')), { once: true });
    });
    request.timer = setTimeout(() => controller.abort(), 10000);
    this.querySelector('[data-refresh-error]')?.remove();
    try {
      const html = await Promise.race([
        fetch(url.href, { signal: controller.signal, redirect: 'error', credentials: 'same-origin' })
          .then(response => {
            if (!response.ok || response.redirected || response.url !== url.href) {
              throw new Error('Invalid status response');
            }
            return response.text();
          }),
        aborted,
      ]);
      if (this._refreshRequest !== request || !this.isConnected ||
          this.getAttribute('check-id') !== checkId || document.getElementById(this.id) !== this) return;
      const template = document.createElement('template');
      template.innerHTML = html;
      const replacement = template.content.firstElementChild;
      if (template.content.children.length !== 1 || !replacement?.matches('health-check') ||
          replacement.id !== `check-${checkId}` || replacement.getAttribute('check-id') !== checkId) {
        throw new Error('Invalid status fragment');
      }
      // Retire a successful owner before replacement triggers disconnect.
      clearTimeout(request.timer);
      request.controller.abort();
      this._refreshRequest = null;
      this._htmxCallInProgress = false;
      this._retryOnConnect = false;
      this.replaceWith(replacement);
      window.htmx?.process(replacement);
    } catch {
      if (this._refreshRequest === request && this.isConnected) this.showRefreshError();
    } finally {
      clearTimeout(request.timer);
      if (this._refreshRequest === request) {
        this._refreshRequest = null;
        this._htmxCallInProgress = false;
      }
    }
  }

  showRefreshError() {
    this.querySelector('[data-refresh-error]')?.remove();
    const message = document.createElement('span');
    message.dataset.refreshError = '';
    message.setAttribute('role', 'status');
    message.textContent = 'Status refresh failed. ';
    const retry = document.createElement('button');
    retry.type = 'button';
    retry.textContent = 'Retry status';
    retry.disabled = true;
    this._retryTimer = setTimeout(() => {
      this._retryTimer = null;
      retry.disabled = false;
    }, 5000);
    retry.addEventListener('click', () => {
      if (!this.isConnected || retry.disabled || this._htmxCallInProgress) return;
      message.remove();
      this.setAttribute('check-mode', 'normal');
      this.handleCheckDue();
    });
    message.append(retry);
    this.append(message);
  }

  cancelRefresh() {
    const request = this._refreshRequest;
    this._refreshRequest = null;
    if (request) {
      clearTimeout(request.timer);
      request.controller.abort();
    }
    clearTimeout(this._htmxTimer);
    clearTimeout(this._retryTimer);
    this._htmxTimer = null;
    this._retryTimer = null;
    this._htmxCallInProgress = false;
    this.querySelector('[data-refresh-error]')?.remove();
  }

  handleThemeChange(event) {
    // Update component styling when the theme changes
    this.applyCurrentTheme();
  }
  
  applyCurrentTheme() {
    // Check if we should use dark theme
    const isDarkTheme = document.body.classList.contains('dark-theme');
    
    if (isDarkTheme) {
      this.classList.add('dark-themed');
    } else {
      this.classList.remove('dark-themed');
    }
    
    // Apply to child web components
    const webComponents = this.querySelectorAll('check-timer, progress-ring');
    webComponents.forEach(component => {
      if (isDarkTheme) {
        component.classList.add('themed');
      } else {
        component.classList.remove('themed');
      }
      
      // Force a refresh by triggering an attribute change
      if (component.shadowRoot) {
        const currentMode = component.getAttribute('mode');
        if (currentMode) {
          component.setAttribute('mode', currentMode);
        }
      }
    });
    
    if (this.debug) console.log(`Applied theme (dark: ${isDarkTheme}) to health check ${this.getAttribute('check-id')}`);
  }
}

// Define the custom element
customElements.define('health-check', HealthCheck);
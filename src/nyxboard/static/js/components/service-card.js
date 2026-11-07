import { announceStatus } from '../status-announcer.js';

/**
 * Custom element for displaying a service with its health checks.
 */
class ServiceCard extends HTMLElement {
  constructor() {
    super();
    
    // Retain the exact listener identity across connect/disconnect cycles.
    this.statusChangeHandler = () => this.updateServiceStatus();
  }
  
  static get observedAttributes() {
    return ['service-status'];
  }
  
  attributeChangedCallback(name, oldValue, newValue) {
    if (oldValue === newValue) return;
    
    if (name === 'service-status') {
      if (this.isConnected) this.announceStatus(newValue);
      const statuses = ['passed', 'failed', 'warning', 'recovering', 'unknown'];
      this.classList.remove(...statuses);
      if (newValue) this.classList.add(newValue);
      // Update the service header class
      const serviceHeader = this.querySelector('.service-header, .service-checks-header');
      if (serviceHeader) {
        // Remove old status classes
        serviceHeader.classList.remove('passed', 'failed', 'warning', 'recovering', 'unknown');
        
        // Add new status class
        if (newValue) {
          serviceHeader.classList.add(newValue);
        }
      }
      
      // Update the status text
      const statusElement = this.querySelector('.service-status');
      if (statusElement && newValue) {
        statusElement.classList.remove(...statuses.map(status => `status-${status}`));
        statusElement.classList.add(`status-${newValue}`);
        statusElement.textContent = newValue.charAt(0).toUpperCase() + newValue.slice(1);
      }
    }
  }
  
  announceStatus(status) {
    announceStatus('service', this.getAttribute('service-id'),
      this.querySelector('.service-link, .service-checks-link, .service-name')?.textContent.trim(), status);
  }

  connectedCallback() {
    this.announceStatus(this.getAttribute('service-status'));
    // Listen for health check status changes to update service status
    this.addEventListener('health-check-status-changed', this.statusChangeHandler);
  }
  
  disconnectedCallback() {
    // Clean up event listeners
    this.removeEventListener('health-check-status-changed', this.statusChangeHandler);
  }
  
  updateServiceStatus() {
    // Get all health checks
    const healthChecks = this.querySelectorAll('health-check');
    if (!healthChecks.length) {
      // If no health checks, set status to unknown
      this.setAttribute('service-status', 'unknown');
      return;
    }
    
    // Collect statuses
    const statuses = Array.from(healthChecks).map(check => check.getAttribute('status'));
    
    // Determine overall status
    let serviceStatus;
    
    if (statuses.includes('failed')) {
      serviceStatus = 'failed';
    } else if (statuses.includes('warning') || statuses.includes('recovering')) {
      serviceStatus = 'warning';
    } else if (statuses.every(status => status === 'passed')) {
      serviceStatus = 'passed';
    } else if (statuses.every(status => status === 'unknown')) {
      serviceStatus = 'unknown';
    } else {
      serviceStatus = 'warning';
    }
    
    this.setAttribute('service-status', serviceStatus);
    // The overview mirrors the grouped checks without another request path.
    const serviceId = this.getAttribute('service-id');
    if (serviceId) {
      document.querySelectorAll('service-card').forEach(card => {
        if (card !== this && card.getAttribute('service-id') === serviceId) {
          card.setAttribute('service-status', serviceStatus);
        }
      });
    }
  }
}

// Define the custom element
customElements.define('service-card', ServiceCard);
/**
 * Custom element for displaying a service with its health checks.
 */
class ServiceCard extends HTMLElement {
  constructor() {
    super();
    
    // We're using a regular DOM element (not Shadow DOM) to allow for HTMX integration
  }
  
  static get observedAttributes() {
    return ['service-status'];
  }
  
  attributeChangedCallback(name, oldValue, newValue) {
    if (oldValue === newValue) return;
    
    if (name === 'service-status') {
      // Update the service header class
      const serviceHeader = this.querySelector('.service-header');
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
        statusElement.textContent = newValue.charAt(0).toUpperCase() + newValue.slice(1);
      }
    }
  }
  
  connectedCallback() {
    // Listen for health check status changes to update service status
    this.addEventListener('health-check-status-changed', this.updateServiceStatus.bind(this));
  }
  
  disconnectedCallback() {
    // Clean up event listeners
    this.removeEventListener('health-check-status-changed', this.updateServiceStatus.bind(this));
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
  }
}

// Define the custom element
customElements.define('service-card', ServiceCard);
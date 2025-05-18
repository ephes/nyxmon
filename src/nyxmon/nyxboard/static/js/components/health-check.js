/**
 * Custom element for displaying a health check with its status, progress, and controls.
 * This component integrates with HTMX for server interactions.
 */
class HealthCheck extends HTMLElement {
  constructor() {
    super();
    
    // We're using a regular DOM element (not Shadow DOM) to allow for HTMX integration
    this.checkDueHandler = this.handleCheckDue.bind(this);
    
    // Debug flag
    this.debug = true;
  }
  
  static get observedAttributes() {
    return ['check-id', 'check-mode', 'next-check', 'check-interval', 'status'];
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
    
    // Initialize child components if needed
    this.updateComponentStatuses();
  }
  
  disconnectedCallback() {
    if (this.debug) console.log(`HealthCheck disconnected: #check-${this.getAttribute('check-id')}`);
    
    // Clean up event listeners
    const progressRing = this.querySelector('progress-ring');
    if (progressRing) {
      progressRing.removeEventListener('check-due', this.checkDueHandler);
      if (this.debug) console.log('Removed check-due event listener from progress-ring');
    }
  }
  
  attributeChangedCallback(name, oldValue, newValue) {
    if (oldValue === newValue) return;
    
    if (this.debug) console.log(`HealthCheck attribute changed: ${name} = ${newValue}`);
    
    // When attributes change, update the corresponding components
    if (name === 'check-mode' || name === 'status' || name === 'next-check' || name === 'check-interval') {
      this.updateComponentStatuses();
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
      if (status) checkTimer.setAttribute('status', status);
      if (nextCheck) checkTimer.setAttribute('next-check', nextCheck);
    }
  }
  
  handleCheckDue(event) {
    const checkId = this.getAttribute('check-id');
    
    // Only proceed if we're not already in "due" mode
    if (this.getAttribute('check-mode') !== 'due') {
      console.log(`Progress reached 100% for check ${checkId}. Transitioning to due mode.`);
      
      // First update our own attribute to immediately reflect the due state
      // This is important to prevent multiple triggers
      this.setAttribute('check-mode', 'due');
      
      // Then use HTMX to update the health check
      if (window.htmx) {
        if (this.debug) console.log(`Triggering HTMX call to /healthchecks/${checkId}/status/`);
        
        // Use a timeout to ensure this doesn't conflict with other DOM operations
        setTimeout(() => {
          window.htmx.ajax('GET', `/healthchecks/${checkId}/status/`, {
            target: `#check-${checkId}`, 
            swap: 'outerHTML'
          });
        }, 10);
      } else {
        console.error('HTMX not loaded, falling back to fetch');
        fetch(`/healthchecks/${checkId}/status/`)
          .then(response => response.text())
          .then(html => {
            // Use a temporary div to create DOM elements
            const tempDiv = document.createElement('div');
            tempDiv.innerHTML = html;
            const newCheckElement = tempDiv.firstChild;
            
            // Replace the element
            this.parentNode.replaceChild(newCheckElement, this);
          })
          .catch(error => {
            console.error('Error fetching check status:', error);
          });
      }
    }
  }
}

// Define the custom element
customElements.define('health-check', HealthCheck);
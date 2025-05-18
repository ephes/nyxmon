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
    return ['check-id', 'check-mode', 'next-check', 'check-interval', 'status', 'theme'];
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
  }
  
  disconnectedCallback() {
    if (this.debug) console.log(`HealthCheck disconnected: #check-${this.getAttribute('check-id')}`);
    
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
    if (name === 'check-mode' || name === 'status' || name === 'next-check' || name === 'check-interval') {
      this.updateComponentStatuses();
    } else if (name === 'theme') {
      this.applyCurrentTheme();
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
    
    // Only proceed if we're not already in "due" mode and this is still in the DOM
    if (this.getAttribute('check-mode') !== 'due' && document.body.contains(this)) {
      console.log(`Progress reached 100% for check ${checkId}. Transitioning to due mode.`);
      
      // First update our own attribute to immediately reflect the due state
      // This is important to prevent multiple triggers
      this.setAttribute('check-mode', 'due');
      
      // Add a flag to prevent multiple HTMX calls
      if (this._htmxCallInProgress) {
        console.log('HTMX call already in progress, skipping');
        return;
      }
      
      // Store the current state of the last-result data to preserve it during processing
      const checkId = this.getAttribute('check-id');
      const lastResultElement = document.querySelector(`#last-result-${checkId}`);
      if (lastResultElement) {
        this._lastResultData = lastResultElement.innerHTML;
        console.log(`Saving last result data for check ${checkId}`);
      }
      
      this._htmxCallInProgress = true;
      
      // Then use HTMX to update the health check
      if (window.htmx) {
        if (this.debug) console.log(`Triggering HTMX call to /healthchecks/${checkId}/status/`);
        
        // Use a timeout with longer delay to reduce chances of conflicts
        setTimeout(() => {
          try {
            if (document.body.contains(this)) {  
              window.htmx.ajax('GET', `/healthchecks/${checkId}/status/`, {
                target: `#check-${checkId}`, 
                swap: 'outerHTML'
              });
            }
          } catch (error) {
            console.error('Error triggering HTMX call:', error);
          } finally {
            // Reset the flag even if there was an error
            this._htmxCallInProgress = false;
          }
        }, 50);
      } else {
        console.error('HTMX not loaded, falling back to fetch');
        fetch(`/healthchecks/${checkId}/status/`)
          .then(response => response.text())
          .then(html => {
            try {
              // Only proceed if this component is still in the DOM
              if (document.body.contains(this)) {
                // Use a temporary div to create DOM elements
                const tempDiv = document.createElement('div');
                tempDiv.innerHTML = html;
                const newCheckElement = tempDiv.firstChild;
                
                // Apply the current theme before inserting
                const isDarkTheme = document.body.classList.contains('dark-theme');
                if (isDarkTheme && newCheckElement) {
                  newCheckElement.classList.add('dark-themed');
                  const webComponents = newCheckElement.querySelectorAll('check-timer, progress-ring');
                  webComponents.forEach(component => component.classList.add('themed'));
                }
                
                // Replace the element
                if (this.parentNode) {
                  this.parentNode.replaceChild(newCheckElement, this);
                }
              }
            } catch (error) {
              console.error('Error updating DOM with fetched content:', error);
            } finally {
              this._htmxCallInProgress = false;
            }
          })
          .catch(error => {
            console.error('Error fetching check status:', error);
            this._htmxCallInProgress = false;
          });
      }
    }
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
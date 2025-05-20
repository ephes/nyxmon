/**
 * Main JavaScript file for the dashboard functionality.
 * Loads all components and initializes the dashboard.
 */

// Import web components
import './components/progress-ring.js';
import './components/check-timer.js';
import './components/health-check.js';
import './components/service-card.js';

/**
 * Initialize the dashboard functionality.
 */
function initDashboard() {
  console.log('Nyxmon Dashboard initialized');
  
  // Listen for HTMX events
  document.body.addEventListener('htmx:afterSwap', function(evt) {
    // console.log('HTMX swap detected:', evt.detail.target);
    
    // Make sure newly added components get properly initialized
    // This shouldn't be necessary with the autoUpgrade feature of web components,
    // but it's included as a fallback in case any initialization is needed
    
    // Example of how to handle a specific element type if needed
    const newHealthCheck = evt.detail.target.closest('health-check');
    if (newHealthCheck) {
      console.log('Health check was swapped with htmx', newHealthCheck.id);
    }
  });
  
  // Add a global event listener for check-due events to debug
  document.body.addEventListener('check-due', function(evt) {
    console.log('Global check-due event received from:', evt.target);
    
    // Fallback handler for check-due events
    // This is a safety measure in case the health-check component doesn't handle the event
    const progressRing = evt.target;
    const healthCheck = progressRing.closest('li[is="health-check"]');
    
    if (healthCheck && healthCheck.getAttribute('check-mode') !== 'due') {
      console.log('Fallback: Progress reached 100% for check', healthCheck.id);
      
      // Get the check ID from the element's ID
      const checkId = healthCheck.getAttribute('check-id');
      if (!checkId) return;
      
      // Update the health check mode to prevent further requests
      healthCheck.setAttribute('check-mode', 'due');
      
      // Send the HTMX request manually
      if (window.htmx) {
        console.log('Fallback: Triggering HTMX request to /healthchecks/' + checkId + '/status/');
        setTimeout(() => {
          window.htmx.ajax('GET', `/healthchecks/${checkId}/status/`, {
            target: `#check-${checkId}`,
            swap: 'outerHTML'
          });
        }, 50);
      }
    }
  });
}

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', initDashboard);
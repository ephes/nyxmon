/**
 * Main JavaScript file for the dashboard functionality.
 * Loads all components and initializes the dashboard.
 */

import './card-interactions.js';

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
  

}

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', initDashboard);
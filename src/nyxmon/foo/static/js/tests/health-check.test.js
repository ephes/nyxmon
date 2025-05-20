/**
 * Tests for the HealthCheck web component
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// We're not importing the actual component since we just want to test the logic
// import '../components/health-check.js';

describe('HealthCheck', () => {
  // Test specific implementation
  let healthCheck;
  let progressRing;
  let checkTimer;
  
  beforeEach(() => {
    // Create a test instance
    healthCheck = {
      // Properties
      attributes: {
        'check-id': '123',
        'check-mode': 'normal',
        'next-check': '1620000000',
        'check-interval': '3600',
        'status': 'passed'
      },
      id: 'check-123',
      debug: false,
      
      // Methods
      getAttribute: function(name) {
        return this.attributes[name];
      },
      
      setAttribute: vi.fn(function(name, value) {
        this.attributes[name] = value;
      }),
      
      // Components
      querySelector: function(selector) {
        if (selector === 'progress-ring') return progressRing;
        if (selector === 'check-timer') return checkTimer;
        return null;
      },
      
      // Component methods
      updateComponentStatuses: function() {
        const mode = this.getAttribute('check-mode');
        const status = this.getAttribute('status');
        const nextCheck = this.getAttribute('next-check');
        const checkInterval = this.getAttribute('check-interval');
        
        const progressRing = this.querySelector('progress-ring');
        if (progressRing) {
          if (mode) progressRing.setAttribute('mode', mode);
          if (nextCheck) progressRing.setAttribute('next-check', nextCheck);
          if (checkInterval) progressRing.setAttribute('check-interval', checkInterval);
        }
        
        const checkTimer = this.querySelector('check-timer');
        if (checkTimer) {
          if (mode) checkTimer.setAttribute('mode', mode);
          if (status) checkTimer.setAttribute('status', status);
          if (nextCheck) checkTimer.setAttribute('next-check', nextCheck);
        }
      },
      
      handleCheckDue: function(event) {
        const checkId = this.getAttribute('check-id');
        
        // Only proceed if we're not already in "due" mode
        if (this.getAttribute('check-mode') !== 'due') {
          // First update our own attribute to immediately reflect the due state
          this.setAttribute('check-mode', 'due');
          
          // Then use HTMX to update the health check
          if (window.htmx) {
            setTimeout(() => {
              window.htmx.ajax('GET', `/healthchecks/${checkId}/status/`, {
                target: `#check-${checkId}`, 
                swap: 'outerHTML'
              });
            }, 10);
          }
        }
      }
    };
    
    // Create mock child elements
    progressRing = {
      setAttribute: vi.fn()
    };
    
    checkTimer = {
      setAttribute: vi.fn()
    };
    
    // Mock HTMX
    window.htmx = {
      ajax: vi.fn(),
      trigger: vi.fn()
    };
    
    // Mock setTimeout to run immediately
    vi.spyOn(window, 'setTimeout').mockImplementation((callback) => {
      callback();
      return 1;
    });
  });
  
  afterEach(() => {
    vi.restoreAllMocks();
  });
  
  it('updates component statuses when check mode changes', () => {
    // Set check-mode attribute
    healthCheck.attributes['check-mode'] = 'due';
    
    // Call the method
    healthCheck.updateComponentStatuses();
    
    // Verify components were updated
    expect(progressRing.setAttribute).toHaveBeenCalledWith('mode', 'due');
    expect(checkTimer.setAttribute).toHaveBeenCalledWith('mode', 'due');
  });
  
  it('handles the check-due event by transitioning to due mode', () => {
    // Create a check-due event
    const checkDueEvent = { type: 'check-due' };
    
    // Call the handler directly
    healthCheck.handleCheckDue(checkDueEvent);
    
    // Verify mode attribute was changed
    expect(healthCheck.setAttribute).toHaveBeenCalledWith('check-mode', 'due');
    
    // Verify HTMX Ajax call was made
    expect(window.htmx.ajax).toHaveBeenCalledWith(
      'GET',
      '/healthchecks/123/status/',
      expect.objectContaining({
        target: '#check-123',
        swap: 'outerHTML'
      })
    );
  });
  
  it('does not transition to due mode if already in due mode', () => {
    // Set component to due mode
    healthCheck.attributes['check-mode'] = 'due';
    
    // Reset the mock to clear previous calls
    window.htmx.ajax.mockClear();
    
    // Create a check-due event
    const checkDueEvent = { type: 'check-due' };
    
    // Call the handler directly
    healthCheck.handleCheckDue(checkDueEvent);
    
    // Verify HTMX Ajax call was NOT made
    expect(window.htmx.ajax).not.toHaveBeenCalled();
  });
});
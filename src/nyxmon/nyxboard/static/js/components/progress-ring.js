/**
 * Custom element for displaying a progress ring with percentage.
 * Used for visualizing the time until the next health check.
 */
class ProgressRing extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    
    // Set up the initial HTML
    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: inline-block;
          position: relative;
          width: 60px;
          height: 60px;
        }
        
        svg {
          transform: rotate(-90deg);
          width: 60px;
          height: 60px;
        }
        
        circle {
          fill: none;
          stroke-width: 4;
          r: 26;
          cx: 30;
          cy: 30;
          stroke-dasharray: 163.36;
          stroke-dashoffset: 163.36;
          transition: stroke-dashoffset 0.3s ease;
        }
        
        .progress-ring-bg {
          stroke: #e5e7eb;
        }
        
        .progress-ring-progress {
          stroke: currentColor;
          stroke-linecap: round;
          opacity: 0.8;
        }
        
        .progress-ring-progress.due {
          stroke: #f87171 !important;
        }
        
        .progress-text {
          position: absolute;
          top: 50%;
          left: 50%;
          transform: translate(-50%, -50%);
          text-align: center;
          font-size: 0.7rem;
          font-weight: 600;
          color: inherit;
        }
      </style>
      
      <svg class="progress-ring">
        <circle class="progress-ring-bg" cx="30" cy="30" r="26"/>
        <circle class="progress-ring-progress" cx="30" cy="30" r="26"/>
      </svg>
      
      <div class="progress-text">
        <span class="progress-percentage">0%</span>
      </div>
    `;
    
    // Get references to elements
    this.progressRing = this.shadowRoot.querySelector('.progress-ring-progress');
    this.progressText = this.shadowRoot.querySelector('.progress-percentage');
    
    // Initialize update interval
    this.updateInterval = null;
    
    // Flag to track if event has been fired
    this._hasFiredDueEvent = false;
    
    // Debug flag
    this.debug = true;
    
    // Bind methods to ensure consistent 'this' context
    this.updateProgress = this.updateProgress.bind(this);
    this.startUpdates = this.startUpdates.bind(this);
    this.stopUpdates = this.stopUpdates.bind(this);
  }
  
  static get observedAttributes() {
    return ['next-check', 'check-interval', 'mode'];
  }
  
  connectedCallback() {
    // Start updating the progress ring
    this.startUpdates();
    
    if (this.debug) {
      console.log(`ProgressRing connected with next-check=${this.getAttribute('next-check')}, interval=${this.getAttribute('check-interval')}, mode=${this.getAttribute('mode')}`);
    }
  }
  
  disconnectedCallback() {
    // Clean up when element is removed
    this.stopUpdates();
    
    if (this.debug) {
      console.log(`ProgressRing disconnected`);
    }
  }
  
  attributeChangedCallback(name, oldValue, newValue) {
    if (oldValue === newValue) return;
    
    if (this.debug) {
      console.log(`ProgressRing attribute changed: ${name} = ${newValue}`);
    }
    
    if (name === 'mode' && newValue === 'due') {
      // Reset the fired event flag when mode is explicitly set to due
      this._hasFiredDueEvent = true;
      this.progressRing.classList.add('due');
      this.updateProgressRing(100);
    } else if (name === 'mode' && newValue !== 'due') {
      // When mode changes from due to normal, remove due class
      this.progressRing.classList.remove('due');
      this._hasFiredDueEvent = false;
      this.updateProgress();
    } else if (name === 'next-check' || name === 'check-interval') {
      // When timing attributes change, reset the fired event flag
      this._hasFiredDueEvent = false;
      this.updateProgress();
    } else {
      // For other attribute changes, update the progress ring
      this.updateProgress();
    }
  }
  
  startUpdates() {
    // Update immediately
    this.updateProgress();
    
    // Then update every second
    this.stopUpdates(); // Clear any existing interval
    this.updateInterval = setInterval(this.updateProgress, 1000);
  }
  
  stopUpdates() {
    if (this.updateInterval) {
      clearInterval(this.updateInterval);
      this.updateInterval = null;
    }
  }
  
  updateProgress() {
    const nextCheck = this.getAttribute('next-check');
    const checkInterval = this.getAttribute('check-interval');
    const mode = this.getAttribute('mode');
    
    if (!nextCheck || !checkInterval) return;
    
    // If in "due" mode, always show 100%
    if (mode === 'due') {
      this._hasFiredDueEvent = true; // Prevent firing while in due mode
      this.updateProgressRing(100);
      return;
    }
    
    const progress = this.calculateProgress(nextCheck, checkInterval);
    this.updateProgressRing(progress);
    
    // Fire the due event when we reach 100% (if not already fired)
    if (progress >= 100 && !this._hasFiredDueEvent) {
      this._hasFiredDueEvent = true;
      
      if (this.debug) {
        console.log('Firing check-due event from progress-ring');
      }
      
      // Use requestAnimationFrame to ensure DOM is fully updated
      requestAnimationFrame(() => {
        // First dispatch standard custom event
        const checkDueEvent = new CustomEvent('check-due', {
          bubbles: true,
          composed: true
        });
        
        this.dispatchEvent(checkDueEvent);
        
        // Then also dispatch an HTMX event that can be triggered directly by HTMX
        const htmxEvent = new CustomEvent('checkDue', {
          bubbles: true,
          composed: true
        });
        
        const healthCheck = this.closest('li[is="health-check"]');
        if (healthCheck) {
          healthCheck.dispatchEvent(htmxEvent);
          if (this.debug) console.log('HTMX checkDue event dispatched to', healthCheck.id);
        }
        
        if (this.debug) {
          console.log('check-due event dispatched successfully');
        }
      });
    }
  }
  
  calculateProgress(nextCheckTimestamp, checkInterval) {
    const now = Math.floor(Date.now() / 1000); // Current time in Unix timestamp
    const nextCheck = parseInt(nextCheckTimestamp);
    const interval = parseInt(checkInterval);
    
    // If next check time is in the past, return 100% progress
    if (nextCheck <= now) {
      return 100;
    }
    
    // Calculate when the last check should have been
    const lastCheck = nextCheck - interval;
    
    // Calculate progress
    const elapsed = now - lastCheck;
    const progress = Math.min(100, Math.max(0, (elapsed / interval) * 100));
    
    return progress;
  }
  
  updateProgressRing(progress) {
    // Update the ring visualization
    const circumference = 2 * Math.PI * 26; // r = 26
    const offset = circumference - (progress / 100) * circumference;
    this.progressRing.style.strokeDashoffset = offset;
    
    // Update the text
    this.progressText.textContent = `${Math.round(progress)}%`;
    
    // Change color based on progress
    if (progress < 50) {
      this.progressRing.style.stroke = "#4ade80"; // green
    } else if (progress < 80) {
      this.progressRing.style.stroke = "#facc15"; // yellow
    } else {
      this.progressRing.style.stroke = "#f87171"; // red
    }
  }
}

// Define the custom element
customElements.define('progress-ring', ProgressRing);
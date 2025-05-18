/**
 * Custom element for displaying the next check time and countdown.
 */
class CheckTimer extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    
    // Set up the initial HTML
    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: block;
          font-size: 0.875rem;
          text-align: right;
          color: #4b5563;
          min-width: 100px;
          background-color: #f9fafb;
          padding: 0.375rem;
          border-radius: 0.375rem;
          border: 1px solid #e5e7eb;
        }
        
        :host(.check-due) {
          background-color: rgba(245, 158, 11, 0.1);
          border: 1px solid #f59e0b;
        }
        
        .next-check-time {
          font-weight: 600;
          display: block;
          margin-bottom: 0.25rem;
          color: #1f2937;
        }
        
        .next-check-countdown {
          font-size: 0.75rem;
          opacity: 0.8;
          font-weight: 500;
        }
        
        .check-status {
          display: block;
          text-align: center;
          font-weight: 600;
        }
        
        .status-processing {
          color: #2563eb;
          font-weight: bold;
          animation: pulse 1.5s infinite;
        }
        
        .status-due {
          color: #f59e0b;
          font-weight: bold;
        }
        
        @keyframes pulse {
          0% { opacity: 1; }
          50% { opacity: 0.5; }
          100% { opacity: 1; }
        }
      </style>
      
      <div class="timer-container">
        <span class="next-check-time"></span>
        <span class="next-check-countdown"></span>
        <span class="check-status"></span>
      </div>
    `;
    
    // Get references to elements
    this.timeContainer = this.shadowRoot.querySelector('.timer-container');
    this.nextCheckTime = this.shadowRoot.querySelector('.next-check-time');
    this.nextCheckCountdown = this.shadowRoot.querySelector('.next-check-countdown');
    this.checkStatus = this.shadowRoot.querySelector('.check-status');
    
    // Initialize update interval
    this.updateInterval = null;
    
    // Debug flag
    this.debug = true;
    
    // Bind methods
    this.updateDisplay = this.updateDisplay.bind(this);
    this.startUpdates = this.startUpdates.bind(this);
    this.stopUpdates = this.stopUpdates.bind(this);
  }
  
  static get observedAttributes() {
    return ['next-check', 'mode', 'status'];
  }
  
  connectedCallback() {
    if (this.debug) {
      console.log(`CheckTimer connected with next-check=${this.getAttribute('next-check')}, mode=${this.getAttribute('mode')}`);
    }
    
    // Start updating the timer
    this.startUpdates();
  }
  
  disconnectedCallback() {
    // Clean up when element is removed
    this.stopUpdates();
    
    if (this.debug) {
      console.log(`CheckTimer disconnected`);
    }
  }
  
  attributeChangedCallback(name, oldValue, newValue) {
    if (oldValue === newValue) return;
    
    if (this.debug) {
      console.log(`CheckTimer attribute changed: ${name} = ${newValue}`);
    }
    
    this.updateDisplay();
  }
  
  startUpdates() {
    // Update immediately
    this.updateDisplay();
    
    // Then update every second
    this.stopUpdates(); // Clear any existing interval
    this.updateInterval = setInterval(this.updateDisplay, 1000);
  }
  
  stopUpdates() {
    if (this.updateInterval) {
      clearInterval(this.updateInterval);
      this.updateInterval = null;
    }
  }
  
  updateDisplay() {
    const nextCheck = this.getAttribute('next-check');
    const mode = this.getAttribute('mode');
    const status = this.getAttribute('status');
    
    if (mode === 'due') {
      // If in "due" mode, show status instead of time
      this.classList.add('check-due');
      this.nextCheckTime.style.display = 'none';
      this.nextCheckCountdown.style.display = 'none';
      this.checkStatus.style.display = 'block';
      
      if (status === 'processing') {
        this.checkStatus.innerHTML = '<span class="status-processing">In progress...</span>';
      } else {
        this.checkStatus.innerHTML = '<span class="status-due">Check due</span>';
      }
    } else {
      // In normal mode, show time and countdown
      this.classList.remove('check-due');
      this.nextCheckTime.style.display = 'block';
      this.nextCheckCountdown.style.display = 'block';
      this.checkStatus.style.display = 'none';
      
      if (nextCheck) {
        // Format the time (only once if not changed)
        if (!this.nextCheckTime._formattedValue || this.nextCheckTime._formattedValue !== nextCheck) {
          this.nextCheckTime.textContent = this.formatNextCheckTime(nextCheck);
          this.nextCheckTime._formattedValue = nextCheck;
        }
        
        // Always update countdown
        this.nextCheckCountdown.textContent = this.formatTimeRemaining(nextCheck);
      }
    }
  }
  
  formatTimeRemaining(nextCheckTimestamp) {
    const nextCheck = parseInt(nextCheckTimestamp);
    const now = Math.floor(Date.now() / 1000);
    const remaining = nextCheck - now;
    
    if (remaining <= 0) return "Due now";
    
    const hours = Math.floor(remaining / 3600);
    const minutes = Math.floor((remaining % 3600) / 60);
    const seconds = remaining % 60;
    
    if (hours > 0) {
      return `${hours}h ${minutes}m`;
    } else if (minutes > 0) {
      return `${minutes}m ${seconds}s`;
    } else {
      return `${seconds}s`;
    }
  }
  
  formatNextCheckTime(nextCheckTimestamp) {
    const nextCheck = new Date(parseInt(nextCheckTimestamp) * 1000);
    const now = new Date();
    
    // If next check is in the past, return "Due now"
    if (nextCheck <= now) {
      return "Due now";
    }
    
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const checkDate = new Date(nextCheck.getFullYear(), nextCheck.getMonth(), nextCheck.getDate());
    
    const timeString = nextCheck.toLocaleTimeString('en-US', {
      hour12: false,
      hour: '2-digit',
      minute: '2-digit'
    });
    
    if (checkDate.getTime() === today.getTime()) {
      return `Today at ${timeString}`;
    } else if (checkDate.getTime() === today.getTime() + 86400000) {
      return `Tomorrow at ${timeString}`;
    } else {
      return nextCheck.toLocaleString('en-US', {
        month: 'short',
        day: 'numeric',
        hour12: false,
        hour: '2-digit',
        minute: '2-digit'
      });
    }
  }
}

// Define the custom element
customElements.define('check-timer', CheckTimer);
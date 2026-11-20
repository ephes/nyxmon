import { parseCheckTimestamp } from './check-timing.js';

/**
 * Custom element for displaying a progress ring with percentage.
 * Used for visualizing the time until the next health check.
 */
class ProgressRing extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    
    // Set up the initial HTML with CSS custom property support
    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: inline-block;
          position: relative;
          width: 60px;
          height: 60px;
        }
        
        .progress-container {
          position: relative;
          width: 60px;
          height: 60px;
          display: flex;
          justify-content: center;
          align-items: center;
        }
        
        svg {
          position: absolute;
          top: 0;
          left: 0;
          width: 60px;
          height: 60px;
          transform: rotate(-90deg);
          z-index: 1;
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
          stroke: var(--progress-ring-bg, #e5e7eb);
        }
        
        .progress-ring-progress {
          stroke: currentColor;
          stroke-linecap: round;
          opacity: 0.8;
        }
        
        .progress-ring-progress.due {
          stroke: var(--color-failed, #f87171) !important;
        }
        
        .progress-text {
          position: relative;
          z-index: 2;
          text-align: center;
          font-size: 0.7rem;
          font-weight: 600;
          color: var(--timer-text-color, inherit);
          /* Added fixed width/height to ensure consistent sizing */
          width: 30px;
          height: 20px;
          display: flex;
          align-items: center;
          justify-content: center;
        }
      </style>
      
      <div class="progress-container">
        <svg class="progress-ring">
          <circle class="progress-ring-bg" cx="30" cy="30" r="26"/>
          <circle class="progress-ring-progress" cx="30" cy="30" r="26"/>
        </svg>
        
        <div class="progress-text">
          <span class="progress-percentage">0%</span>
        </div>
      </div>
    `;
    
    // Get references to elements
    this.progressRing = this.shadowRoot.querySelector('.progress-ring-progress');
    this.progressText = this.shadowRoot.querySelector('.progress-percentage');
    
    // Initialize update interval
    this.updateInterval = null;
    
    // Flag to track if event has been fired
    this._hasFiredDueEvent = false;
    this._dueTimer = null;
    
    // Debug flag
    this.debug = false;
    
    // Bind methods to ensure consistent 'this' context
    this.updateProgress = this.updateProgress.bind(this);
    this.startUpdates = this.startUpdates.bind(this);
    this.stopUpdates = this.stopUpdates.bind(this);
    this.handleThemeChange = this.handleThemeChange.bind(this);
  }
  
  static get observedAttributes() {
    return ['next-check', 'check-interval', 'mode'];
  }
  
  connectedCallback() {
    // Start updating the progress ring
    this.startUpdates();
    
    // Listen for theme changes
    document.addEventListener('theme-changed', this.handleThemeChange);
    
    if (this.debug) {
      console.log(`ProgressRing connected with next-check=${this.getAttribute('next-check')}, interval=${this.getAttribute('check-interval')}, mode=${this.getAttribute('mode')}`);
    }
  }
  
  disconnectedCallback() {
    // Clean up when element is removed
    this.stopUpdates();
    this.cancelDueEvent();
    
    // Remove event listeners
    document.removeEventListener('theme-changed', this.handleThemeChange);
    
    if (this.debug) {
      console.log(`ProgressRing disconnected`);
    }
  }
  
  handleThemeChange(event) {
    // Apply theme by updating CSS variables regardless of themed class
    // Get theme values based on current body class
    const isDark = document.body.classList.contains('dark-theme');
    
    // Add/remove themed class
    if (isDark) {
      this.classList.add('themed');
    } else {
      this.classList.remove('themed');
    }
    
    // Force component to update its ring colors with the new theme
    // Direct values for better reliability
    const ringBgColor = isDark ? '#444' : '#e5e7eb';
    this.style.setProperty('--progress-ring-bg', ringBgColor);
    
    // Force a redraw when theme changes
    this.updateProgress();
    
    if (this.debug) {
      console.log(`Applied theme to progress-ring: ${isDark ? 'dark' : 'light'}`);
    }
  }
  
  attributeChangedCallback(name, oldValue, newValue) {
    if (oldValue === newValue) return;
    this.cancelDueEvent();
    
    if (this.debug) {
      console.log(`ProgressRing attribute changed: ${name} = ${newValue}`);
    }
    
    this.updateProgress();
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
    const progress = this.readProgress();
    const dueMode = this.getAttribute('mode') === 'due';
    this.progressRing.classList.toggle('due', dueMode && progress !== null);

    if (progress === null || dueMode) {
      this.cancelDueEvent();
      this.updateProgressRing(progress === null ? 0 : 100);
      return;
    }

    this.updateProgressRing(progress);
    
    // One notification for the current connected schedule. Timing/mode edits
    // and disconnect cancel this owned callback before it can dispatch.
    if (progress >= 100 && !this._hasFiredDueEvent && this._dueTimer === null && this.isConnected) {
      this._dueTimer = setTimeout(() => {
        this._dueTimer = null;
        if (!this.isConnected || this.getAttribute('mode') === 'due' ||
            this.readProgress() !== 100) return;
        this._hasFiredDueEvent = true;
        this.dispatchEvent(new CustomEvent('check-due', { bubbles: true, composed: true }));
      }, 100);
    }
  }

  cancelDueEvent() {
    clearTimeout(this._dueTimer);
    this._dueTimer = null;
    this._hasFiredDueEvent = false;
  }

  readProgress() {
    const nextCheck = parseCheckTimestamp(this.getAttribute('next-check'));
    const intervalAttribute = this.getAttribute('check-interval');
    if (nextCheck === null || !/^\d+$/.test(intervalAttribute)) return null;

    const interval = Number(intervalAttribute);
    if (!Number.isSafeInteger(interval) || interval <= 0) return null;

    return this.calculateProgress(nextCheck, interval);
  }

  calculateProgress(nextCheckTimestamp, checkInterval) {
    const remaining = nextCheckTimestamp - Math.floor(Date.now() / 1000);
    if (remaining <= 0) return 100;

    const elapsed = checkInterval - remaining;
    return Math.max(0, (elapsed / checkInterval) * 100);
  }

  updateProgressRing(progress) {
    // Update the ring visualization
    const circumference = 2 * Math.PI * 26; // r = 26
    const offset = circumference - (progress / 100) * circumference;
    this.progressRing.style.strokeDashoffset = offset;
    
    // Update the text
    this.progressText.textContent = `${Math.round(progress)}%`;
    
    // Get custom properties considering the themed state
    const getColorProperty = (prop, fallback) => {
      // Check if the component is in themed mode
      const isThemed = this.classList.contains('themed');
      const isDarkTheme = document.body.classList.contains('dark-theme');
      const useThemedColors = isThemed && isDarkTheme;
      
      const style = getComputedStyle(document.documentElement);
      const value = style.getPropertyValue(prop);
      
      if (useThemedColors && prop === '--progress-ring-bg') {
        // Return a darker background for the progress ring in dark mode
        return '#555';
      }
      
      return value || fallback;
    };
    
    // Change color based on progress
    if (progress < 50) {
      this.progressRing.style.stroke = getColorProperty('--color-passed', '#4ade80');
    } else if (progress < 80) {
      this.progressRing.style.stroke = getColorProperty('--color-warning', '#facc15');
    } else {
      this.progressRing.style.stroke = getColorProperty('--color-failed', '#f87171');
    }
    
    // Update background ring color
    const bgRing = this.shadowRoot.querySelector('.progress-ring-bg');
    if (bgRing) {
      bgRing.style.stroke = getColorProperty('--progress-ring-bg', '#e5e7eb');
    }
  }
}

// Define the custom element
customElements.define('progress-ring', ProgressRing);
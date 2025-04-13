# Frontend Documentation

## Health Check Status Display

The frontend displays the health check status in a visual dashboard, grouping health checks by their associated services.

### Status States

Health checks can have the following status states:

1. **Passed** (🟢 Green)
   - All recent check results are successful
   - UI Representation: Green background (#4ade80)

2. **Failed** (🔴 Red)
   - The most recent check result is an error
   - UI Representation: Red background (#f87171)

3. **Warning** (🟠 Orange)
   - Mixed results with some recent failures
   - UI Representation: Orange/Amber background (#fbbf24)

4. **Recovering** (🟡 Yellow)
   - The most recent check is successful, but there were recent failures
   - UI Representation: Yellow background (#facc15)

5. **Unknown** (⚪ Gray)
   - No check results available
   - UI Representation: Gray background (#d1d5db)

### Status Calculation Logic

The status of a health check is calculated by analyzing the recent check results:

1. If no results exist, the status is **Unknown**
2. If the most recent result is an error, the status is **Failed**
3. If the most recent result is OK, but there were errors in recent history, the status is **Recovering**
4. If there's a mix of OK and error results (but not in the pattern for "Recovering"), the status is **Warning**
5. If all recent results are OK, the status is **Passed**

The "recent history" is defined as the last 5 check results.

### Service Status

A service's status is derived from its health checks:

1. If any health check is **Failed**, the service is **Failed**
2. If any health check is **Warning** or **Recovering**, the service is **Warning**
3. If all health checks are **Passed**, the service is **Passed**
4. If all health checks are **Unknown**, the service is **Unknown**

## Dashboard Layout

The dashboard presents services in a list, with each service's health checks displayed as cards or rows with appropriate color coding. The layout is responsive and designed to provide quick visual indication of the system's health.
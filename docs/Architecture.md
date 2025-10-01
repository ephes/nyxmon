# Architecture Document - Minimalistic Monitoring Tool

## Overview

This document outlines the architecture for a minimalistic monitoring tool implemented as a Python library. The tool follows a clean architecture approach with an event-driven core and separation between the library, agents, and frontend components.

## System Components

### Core Library

The core library contains the business logic for monitoring and is published as a PyPI package using `uv` for package management.

#### Components

1. **Domain Model**
   - `Check`: Represents a monitoring check with parameters
   - `Result`: Represents the outcome of an executed check
   - `Service`: Represents a monitored service

2. **Message Bus**
   - Central event dispatcher
   - Handles commands and events
   - Implements publish-subscribe pattern

3. **Commands**
   - `ExecuteChecks`: Triggers execution of pending checks
   - `RegisterCheck`: Registers a new check
   - `DeleteCheck`: Removes a check

4. **Events**
   - `CheckSucceeded`: Emitted when a check succeeds
   - `CheckFailed`: Emitted when a check fails
   - `ServiceStatusChanged`: Emitted when service status changes

5. **Command Handlers**
   - Execute domain logic
   - Validate input
   - Emit events

6. **Check Executors**
   - `HttpCheckExecutor`: Performs HTTP requests and reports status (current implementation)
   - `DnsCheckExecutor`: Resolves DNS records and validates expected IPs
   - *Planned:* Additional executors (JSON validation, ping, metrics) will extend the same interface in future iterations

7. **Repository Interfaces**
   - `CheckRepository`: Interface for storing and retrieving checks
   - `ResultRepository`: Interface for storing and retrieving check results

### Agent

The agent executes checks and has no direct knowledge of the database implementation.

#### Components

1. **Check Scheduler**
   - Retrieves checks from repository
   - Schedules check execution

2. **Check Runner**
   - Executes checks using appropriate check executors
   - Publishes check results
   - See `docs/async-check-runner.md` for a detailed walkthrough of the asynchronous runner implementation
   - See `docs/concurrency-overview.md` for a high-level description of the runtime threading & event-loop model

3. **Repository Implementations**
   - Concrete implementation of repository interfaces for SQLite

### Frontend (Django)

The frontend is implemented as a Django application and provides a user interface for monitoring and configuration.

#### Components

1. **Django Models**
   - Maps to database tables
   - Implements persistence for checks and results

2. **Views**
   - Dashboard: Overview of service status
   - Check Management: Create, update, delete checks
   - Results: View check execution history

## Data Flow

1. **Check Creation**
   - User creates check via Django frontend
   - Check is persisted in database via Django ORM
   - `RegisterCheck` command is published

2. **Check Execution**
   - Agent fetches checks from repository
   - Agent issues `ExecuteCheck` for each check
   - Command handler executes check via appropriate executor
   - `CheckExecuted` event is emitted with result
   - Result is stored in repository
   - If service status changes, `ServiceStatusChanged` event is emitted

3. **Monitoring**
   - Frontend queries database for current status
   - Dashboard displays service health

### Message Bus Concept

The message bus will:
1. Register handlers for commands and events
2. Route commands to appropriate handlers
3. Distribute events to all subscribers
4. Support synchronous and asynchronous execution models

### Agent Concept

The agent will:
1. Connect to the repository to fetch checks
2. Use the core library to execute checks
3. Store results back to the repository
4. Handle scheduling and retries

### Django Frontend Concept

The Django frontend will:
1. Provide database models for checks and results
2. Use Django ORM for database access
3. Render dashboard views of monitoring status
4. Support CRUD operations for check configurations

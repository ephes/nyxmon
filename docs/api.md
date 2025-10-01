# API Reference

## Core Library

### Domain Model

#### Domain Models

Domain models for checks, results, and services.

```{eval-rst}
.. automodule:: nyxmon.domain.models
   :members:
   :undoc-members:
   :show-inheritance:
```

### Message Bus

Central event dispatcher for commands and events.

```{eval-rst}
.. automodule:: nyxmon.service_layer.message_bus
   :members:
   :undoc-members:
   :show-inheritance:
```

### Check Executors

#### HTTP Check Executor

```{eval-rst}
.. automodule:: nyxmon.adapters.runner.executors.http_executor
   :members:
   :undoc-members:
   :show-inheritance:
```

#### DNS Check Executor

```{eval-rst}
.. automodule:: nyxmon.adapters.runner.executors.dns_executor
   :members:
   :undoc-members:
   :show-inheritance:
```

### Repository Interfaces

```{eval-rst}
.. automodule:: nyxmon.adapters.repositories.interface
   :members:
   :undoc-members:
   :show-inheritance:
```

## Runner

### Async Check Runner

```{eval-rst}
.. automodule:: nyxmon.adapters.runner.async_runner
   :members:
   :undoc-members:
   :show-inheritance:
```

## Notifications

```{eval-rst}
.. automodule:: nyxmon.adapters.notification
   :members:
   :undoc-members:
   :show-inheritance:
```

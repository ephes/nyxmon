"""Check executor registry and interfaces."""

from typing import Any, Callable, Dict, List, Protocol

from ....domain import Check, Result


class CheckExecutor(Protocol):
    """Protocol for check executors.

    All check executors must implement this interface.
    """

    async def execute(self, check: Check) -> Result:
        """Execute a check and return a Result.

        Args:
            check: The check to execute

        Returns:
            Result containing the outcome of the check
        """
        ...

    async def aclose(self) -> None:
        """Clean up executor resources.

        Optional cleanup method for executors that manage resources.
        Executors without resources can omit this or implement as no-op.
        """
        ...


class UnknownCheckTypeError(Exception):
    """Raised when attempting to execute a check with no registered executor."""

    def __init__(self, check_type: str, registered_types: List[str]):
        self.check_type = check_type
        self.registered_types = registered_types
        super().__init__(
            f"No executor registered for check type '{check_type}'. "
            f"Registered types: {', '.join(registered_types) or 'none'}"
        )


# Type for executor factories: functions that create executor instances
ExecutorFactory = Callable[[Any], CheckExecutor]


class ExecutorRegistry:
    """Registry for check executors.

    Provides a pluggable architecture for different check types.
    Supports both direct executor instances and factory functions.
    """

    def __init__(self) -> None:
        """Initialize the executor registry."""
        self._factories: Dict[str, ExecutorFactory] = {}
        self._instances: Dict[str, CheckExecutor] = {}

    def register_factory(self, check_type: str, factory: ExecutorFactory) -> None:
        """Register an executor factory for a check type.

        Args:
            check_type: The type of check (e.g., "http", "dns")
            factory: A callable that creates executor instances
        """
        self._factories[check_type] = factory
        # Clear any cached instance when factory is updated
        self._instances.pop(check_type, None)

    def register(self, check_type: str, executor: CheckExecutor) -> None:
        """Register an executor instance for a check type.

        Args:
            check_type: The type of check (e.g., "http", "dns")
            executor: The executor instance to handle this check type
        """
        # Create a factory that returns the same instance
        self._factories[check_type] = lambda _: executor
        self._instances[check_type] = executor

    def get_executor(self, check_type: str) -> CheckExecutor:
        """Get or create an executor for a check type.

        Args:
            check_type: The type of check

        Returns:
            The executor instance

        Raises:
            UnknownCheckTypeError: If no factory is registered for this type
        """
        if check_type not in self._factories:
            raise UnknownCheckTypeError(check_type, self.list_registered_types())

        # Return cached instance if available
        if check_type in self._instances:
            return self._instances[check_type]

        # Create new instance from factory
        executor = self._factories[check_type](None)
        self._instances[check_type] = executor
        return executor

    def list_registered_types(self) -> List[str]:
        """List all registered check types.

        Returns:
            List of check types that have registered executors
        """
        return list(self._factories.keys())

    async def aclose_all(self) -> None:
        """Close all instantiated executors.

        Calls aclose() on all executor instances that support it.
        """
        for executor in self._instances.values():
            if hasattr(executor, "aclose"):
                await executor.aclose()
        self._instances.clear()


__all__ = [
    "CheckExecutor",
    "ExecutorRegistry",
    "ExecutorFactory",
    "UnknownCheckTypeError",
]

import time
from typing import Literal, TypeAlias

from .events import Event

OK: Literal["ok"] = "ok"
ERROR: Literal["error"] = "error"

Status: TypeAlias = Literal["ok", "error"]


class Result:
    def __init__(
        self, *, result_id: int | None = None, check_id: int, status: Status, data: dict
    ) -> None:
        self.result_id = result_id
        self.check_id = check_id
        self.status = status
        self.data = data
        self.events: list["Event"] = []

    def __repr__(self) -> str:
        return f"Result(result_id={self.result_id}, check_id={self.check_id} status={self.status}, data={self.data})"


class Check:
    def __init__(
        self,
        *,
        check_id: int,
        service_id: int,
        name: str = "",
        check_type: str,
        url: str,
        check_interval: int = 300,
        next_check_time: int = 0,
        processing_started_at: int = 0,
        status: str = "idle",
        data: dict,
    ) -> None:
        self.check_id = check_id
        self.service_id = service_id
        self.name = name
        self.check_type = check_type
        self.url = url
        self.check_interval = check_interval
        self.next_check_time = next_check_time
        self.processing_started_at = processing_started_at
        self.status = status
        self.data = data
        self.events: list["Event"] = []

        # Will be populated when check is executed
        self.result: "Result" = None  # type: ignore

    def __repr__(self) -> str:
        return f"Check(check={self.check_id}, name='{self.name}', service_id={self.service_id} url={self.url})"

    def execute(self) -> None:
        # Logic to execute the check
        pass

    def schedule_next_check(self) -> None:
        """Schedule the next execution of this check."""
        current_time = int(time.time())
        check_interval = self.data.get("check_interval", 300)  # Default to 5 minutes

        self.next_check_time = current_time + check_interval
        self.status = "idle"
        self.processing_started_at = 0


class CheckResult:
    def __init__(self, check: Check, result: Result) -> None:
        self.check = check
        self.result = result
        self.events: list["Event"] = []

    @property
    def passed(self) -> bool:
        return self.result.status == OK

    @property
    def should_notify(self) -> bool:
        return self.result.status == ERROR


class Service:
    def __init__(self, *, service_id: int, data: dict) -> None:
        self.service_id = service_id
        self.data = data
        self.events: list["Event"] = []

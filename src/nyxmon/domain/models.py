import time
from typing import Literal, TypeAlias

from .events import Event


class ResultStatus:
    OK: Literal["ok"] = "ok"
    ERROR: Literal["error"] = "error"


ResultStatusType: TypeAlias = Literal["ok", "error"]


class StatusChoices:
    PASSED: Literal["passed"] = "passed"
    FAILED: Literal["failed"] = "failed"
    WARNING: Literal["warning"] = "warning"
    RECOVERING: Literal["recovering"] = "recovering"
    UNKNOWN: Literal["unknown"] = "unknown"

    @classmethod
    def get_css_class(cls, status: str) -> str:
        css_classes = {
            cls.PASSED: "status-passed",
            cls.FAILED: "status-failed",
            cls.WARNING: "status-warning",
            cls.RECOVERING: "status-recovering",
            cls.UNKNOWN: "status-unknown",
        }
        return css_classes.get(status, "")


StatusType: TypeAlias = Literal["passed", "failed", "warning", "recovering", "unknown"]


class Result:
    def __init__(
        self,
        *,
        result_id: int | None = None,
        check_id: int,
        status: ResultStatusType,
        data: dict,
    ) -> None:
        self.result_id = result_id
        self.check_id = check_id
        self.status = status
        self.data = data
        self.events: list["Event"] = []

    def __repr__(self) -> str:
        return f"Result(result_id={self.result_id}, check_id={self.check_id} status={self.status}, data={self.data})"


class CheckStatus:
    IDLE: Literal["idle"] = "idle"
    PROCESSING: Literal["processing"] = "processing"


CheckStatusType: TypeAlias = Literal["idle", "processing"]


class CheckType:
    HTTP: Literal["http"] = "http"
    JSON_HTTP: Literal["json-http"] = "json-http"
    TCP: Literal["tcp"] = "tcp"
    PING: Literal["ping"] = "ping"
    DNS: Literal["dns"] = "dns"
    CUSTOM: Literal["custom"] = "custom"


CheckTypeType: TypeAlias = Literal["http", "json-http", "tcp", "ping", "dns", "custom"]


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
        status: CheckStatusType = CheckStatus.IDLE,
        disabled: bool = False,
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
        self.disabled = disabled
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

        self.next_check_time = current_time + self.check_interval
        self.status = CheckStatus.IDLE
        self.processing_started_at = 0


class CheckResult:
    def __init__(self, check: Check, result: Result) -> None:
        self.check = check
        self.result = result
        self.events: list["Event"] = []

    @property
    def passed(self) -> bool:
        return self.result.status == ResultStatus.OK

    @property
    def should_notify(self) -> bool:
        return self.result.status == ResultStatus.ERROR


class Service:
    def __init__(self, *, service_id: int, data: dict) -> None:
        self.service_id = service_id
        self.data = data
        self.events: list["Event"] = []

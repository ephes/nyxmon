from typing import TYPE_CHECKING, Literal, TypeAlias

if TYPE_CHECKING:
    from .events import Event


OK: Literal["ok"] = "ok"
ERROR: Literal["error"] = "error"

Status: TypeAlias = Literal["ok", "error"]


class Result:
    def __init__(self, *, result_id: str, status: Status, data: dict) -> None:
        self.result_id = result_id
        self.status = status
        self.data = data
        self.events: list["Event"] = []

    def __repr__(self) -> str:
        return f"Result(result_id={self.result_id}, status={self.status}, data={self.data})"


class Check:
    def __init__(self, *, check_id: str, data: dict) -> None:
        self.check_id = check_id
        self.data = data
        self.events: list["Event"] = []
        self.result = None

    def execute(self) -> None:
        # Logic to execute the check
        pass


class Service:
    def __init__(self, *, service_id: str, data: dict) -> None:
        self.service_id = service_id
        self.data = data
        self.events: list["Event"] = []

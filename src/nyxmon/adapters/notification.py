import logging
import os
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx
from anyio.from_thread import BlockingPortalProvider

from ..domain.models import Check, Result, Service

logger = logging.getLogger(__name__)


class Notifier(Protocol):
    """Interface for notification services."""

    def notify_check_failed(self, check: Check, result: Result) -> None:
        """Notify about a failed check."""
        ...

    def notify_service_status_changed(self, service: Service, status: str) -> None:
        """Notify about a service status change."""
        ...


class AsyncTelegramNotifier(Notifier):
    def __init__(self, token: str | None = None, chat_id: str | None = None):
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
        if not self.token or not self.chat_id:
            logger.warning("Telegram notifier initialized without token or chat_id")
        self.url = (
            f"https://api.telegram.org/bot{self.token}/sendMessage"
            if self.token
            else ""
        )
        self.portal_provider: BlockingPortalProvider | None = None
        self.opsgate_submit_base_url = (
            os.environ.get("OPSGATE_SUBMIT_BASE_URL", "").strip().rstrip("/")
        )
        self.opsgate_submit_token = os.environ.get("OPSGATE_SUBMIT_TOKEN", "").strip()
        self.opsgate_approval_base_url = (
            os.environ.get("OPSGATE_APPROVAL_BASE_URL", "").strip().rstrip("/")
            or self.opsgate_submit_base_url
        )
        self.opsgate_ticket_expires_seconds = self._parse_positive_int(
            os.environ.get("OPSGATE_TICKET_EXPIRES_SECONDS"),
            default=14400,
            env_name="OPSGATE_TICKET_EXPIRES_SECONDS",
        )
        self.opsgate_submit_timeout_seconds = float(
            self._parse_positive_int(
                os.environ.get("OPSGATE_SUBMIT_TIMEOUT_SECONDS"),
                default=10,
                env_name="OPSGATE_SUBMIT_TIMEOUT_SECONDS",
            )
        )
        self.opsgate_include_warnings = self._parse_bool(
            os.environ.get("OPSGATE_SUBMIT_INCLUDE_WARNINGS"),
            default=False,
        )

    def set_portal_provider(self, portal_provider: BlockingPortalProvider) -> None:
        """Set the portal provider for async operations."""
        self.portal_provider = portal_provider

    @staticmethod
    def _parse_positive_int(value: str | None, *, default: int, env_name: str) -> int:
        if value is None or value.strip() == "":
            return default
        try:
            parsed = int(value.strip())
        except ValueError:
            logger.warning("%s is not an integer, using default %s", env_name, default)
            return default
        if parsed <= 0:
            logger.warning("%s must be > 0, using default %s", env_name, default)
            return default
        return parsed

    @staticmethod
    def _parse_bool(value: str | None, *, default: bool) -> bool:
        if value is None:
            return default
        normalized = value.strip().lower()
        if normalized == "":
            return default
        return normalized in {"1", "true", "yes", "on"}

    def _opsgate_enabled(self) -> bool:
        return bool(self.opsgate_submit_base_url and self.opsgate_submit_token)

    @staticmethod
    def _isoformat_z(value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _safe_json_payload(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        return {"repr": repr(value)}

    @staticmethod
    def _json_text(value: Any) -> str:
        try:
            return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)
        except TypeError:
            return json.dumps(
                {"repr": repr(value)}, indent=2, sort_keys=True, ensure_ascii=False
            )

    def _build_opsgate_prompt(self, check: Check, result: Result) -> str:
        summary_lines = [
            "# Objective",
            "Investigate and remediate this Nyxmon alert.",
            "",
            "## Alert Context",
            f"- Check ID: {check.check_id}",
            f"- Check Name: {check.name or 'Unnamed Check'}",
            f"- Check Type: {check.check_type}",
            f"- Check URL: {check.url}",
            f"- Result Status: {result.status}",
            "",
            "## Latest Result Data",
            "```json",
            self._json_text(self._safe_json_payload(result.data)),
            "```",
            "",
            "## Required Output",
            "- Identify root cause.",
            "- Propose/execute the smallest safe remediation.",
            "- Summarize what changed and what remains risky.",
        ]
        return "\n".join(summary_lines)

    def _build_opsgate_ticket_payload(
        self, check: Check, result: Result
    ) -> dict[str, Any]:
        expires_at = self._isoformat_z(
            datetime.now(tz=UTC)
            + timedelta(seconds=self.opsgate_ticket_expires_seconds)
        )
        check_name = check.name or f"Check {check.check_id}"
        severity = "critical" if result.status == "error" else "warning"
        return {
            "title": f"Nyxmon {severity} alert: {check_name}",
            "summary": f"Nyxmon detected a {severity} state for check {check_name} ({check.url}).",
            "task_ref": f"nyxmon-check-{check.check_id}",
            "execution_plan": [
                {
                    "role": "investigator",
                    "agent": "codex",
                    "prompt_markdown": self._build_opsgate_prompt(check, result),
                }
            ],
            "context": {
                "producer": "nyxmon",
                "check": {
                    "check_id": check.check_id,
                    "name": check.name,
                    "check_type": check.check_type,
                    "url": check.url,
                    "service_id": check.service_id,
                },
                "result": {
                    "status": result.status,
                    "data": self._safe_json_payload(result.data),
                },
            },
            "expires_at": expires_at,
        }

    def _build_approval_url(self, ticket_id: str) -> str:
        return f"{self.opsgate_approval_base_url}/tickets/{ticket_id}"

    @staticmethod
    def _escape_markdown_link_url(url: str) -> str:
        return url.replace("\\", "\\\\").replace(")", "\\)")

    async def _create_opsgate_ticket(
        self, check: Check, result: Result
    ) -> dict[str, str] | None:
        if not self._opsgate_enabled():
            return None
        payload = self._build_opsgate_ticket_payload(check, result)
        endpoint = f"{self.opsgate_submit_base_url}/api/v1/tickets"
        headers = {"Authorization": f"Bearer {self.opsgate_submit_token}"}
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    endpoint,
                    headers=headers,
                    json=payload,
                    timeout=self.opsgate_submit_timeout_seconds,
                )
        except Exception as exc:
            logger.error(
                "OpsGate ticket submit failed for check_id=%s: %s", check.check_id, exc
            )
            return {"status": "error"}

        body: dict[str, Any] = {}
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                body = parsed
        except Exception:
            body = {}

        if response.status_code == 201:
            ticket_id = str(body.get("id", "")).strip()
            if not ticket_id:
                logger.error(
                    "OpsGate ticket submit succeeded without ticket id for check_id=%s",
                    check.check_id,
                )
                return {"status": "error"}
            return {"status": "created", "ticket_id": ticket_id}

        if response.status_code == 409 and body.get("error") == "duplicate_open_ticket":
            logger.info(
                "OpsGate duplicate open ticket ignored for check_id=%s task_ref=%s",
                check.check_id,
                f"nyxmon-check-{check.check_id}",
            )
            return {"status": "duplicate"}

        logger.error(
            "OpsGate ticket submit returned status=%s for check_id=%s body=%s",
            response.status_code,
            check.check_id,
            body,
        )
        return {"status": "error"}

    async def async_send(self, text: str, high_priority: bool = False) -> None:
        """Send a message via Telegram asynchronously."""
        if not self.token or not self.chat_id or not self.url:
            logger.warning("Cannot send Telegram notification: missing credentials")
            return

        try:
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "MarkdownV2",
                "disable_notification": not high_priority,
            }
            async with httpx.AsyncClient() as client:
                resp = await client.post(self.url, data=payload, timeout=10.0)
                resp.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to send Telegram notification: {e}")

    @staticmethod
    def escape_markdown_v2(text: str) -> str:
        """Escape special characters for Telegram's MarkdownV2 format."""
        # Characters that need escaping in MarkdownV2: _ * [ ] ( ) ~ ` > # + - = | { } . !
        special_chars = [
            "_",
            "*",
            "[",
            "]",
            "(",
            ")",
            "~",
            "`",
            ">",
            "#",
            "+",
            "-",
            "=",
            "|",
            "{",
            "}",
            ".",
            "!",
        ]
        escaped_text = text
        for char in special_chars:
            escaped_text = escaped_text.replace(char, f"\\{char}")
        return escaped_text

    async def async_notify_check_failed(self, check: Check, result: Result) -> None:
        """Notify about a failed check asynchronously."""
        error_msg = result.data.get("error_msg", "Unknown error")
        error_type = result.data.get("error_type", "")
        status_code = result.data.get("status_code", "")

        # Determine severity from result status (ERROR=critical, WARNING=warning)
        is_critical = result.status == "error"

        # Escape all text for MarkdownV2
        escaped_name = (
            self.escape_markdown_v2(check.name) if check.name else "Unnamed Check"
        )
        escaped_url = self.escape_markdown_v2(check.url)
        escaped_error_msg = self.escape_markdown_v2(str(error_msg))
        escaped_error_type = self.escape_markdown_v2(str(error_type))

        # Use different emoji and title based on severity
        if is_critical:
            message = "🔴 *Check Failed \\(Critical\\)*\n"
        else:
            message = "⚠️ *Check Warning*\n"

        message += f"Name: {escaped_name}\n"
        message += f"URL: {escaped_url}\n"
        if status_code:
            message += f"Status: {status_code}\n"
        if error_type:
            message += f"Error Type: {escaped_error_type}\n"
        message += f"Error: {escaped_error_msg}"

        ticket_info: dict[str, str] | None = None
        if is_critical or self.opsgate_include_warnings:
            ticket_info = await self._create_opsgate_ticket(check, result)
            if ticket_info and ticket_info.get("status") == "created":
                ticket_id = ticket_info["ticket_id"]
                escaped_ticket_id = self.escape_markdown_v2(ticket_id)
                approval_url = self._build_approval_url(ticket_id)
                escaped_approval_url = self._escape_markdown_link_url(approval_url)
                message += "\n\n🛠 *OpsGate Approval Needed*"
                message += f"\nTicket: `{escaped_ticket_id}`"
                message += f"\n[Open approval page]({escaped_approval_url})"
            elif ticket_info and ticket_info.get("status") == "duplicate":
                message += "\n\n🛠 *OpsGate*"
                message += (
                    "\nAn open remediation ticket already exists for this check\\."
                )
            elif ticket_info and ticket_info.get("status") == "error":
                message += "\n\n🛠 *OpsGate*"
                message += "\nTicket creation failed; please create one manually\\."

        # Only use high priority (with sound) for critical failures
        await self.async_send(message, high_priority=is_critical)

    async def async_notify_service_status_changed(
        self, service: Service, status: str
    ) -> None:
        """Notify about a service status change asynchronously."""
        service_name = service.data.get("name", f"Service {service.service_id}")
        escaped_service_name = self.escape_markdown_v2(service_name)
        escaped_status = self.escape_markdown_v2(status)

        emoji = (
            "🔴"
            if status.lower() == "down"
            else "🟢"
            if status.lower() == "up"
            else "⚠️"
        )
        message = f"{emoji} *Service Status Changed*\n"
        message += f"Service: {escaped_service_name}\n"
        message += f"Status: {escaped_status}"

        await self.async_send(message, high_priority=True)

    # Sync methods that call async methods through the portal
    def notify_check_failed(self, check: Check, result: Result) -> None:
        """Notify about a failed check."""
        if self.portal_provider is None:
            logger.warning("Cannot send notification: portal provider not set")
            return

        with self.portal_provider as portal:
            portal.call(self.async_notify_check_failed, check, result)

    def notify_service_status_changed(self, service: Service, status: str) -> None:
        """Notify about a service status change."""
        if self.portal_provider is None:
            logger.warning("Cannot send notification: portal provider not set")
            return

        with self.portal_provider as portal:
            portal.call(self.async_notify_service_status_changed, service, status)


class LoggingNotifier(Notifier):
    """A simple notifier that logs messages to the console."""

    def notify_check_failed(self, check: Check, result: Result) -> None:
        """Log a failed check notification."""
        check_name = check.name if check.name else f"Check {check.check_id}"
        logger.error(
            f"Check failed: {check_name} (ID: {check.check_id}), Result: {result}"
        )

    def notify_service_status_changed(self, service: Service, status: str) -> None:
        """Log a service status change notification."""
        logger.info(f"Service status changed: {service.service_id}, Status: {status}")

    def set_portal_provider(self, portal_provider: BlockingPortalProvider) -> None:
        """Set the portal provider for async operations."""
        self._portal_provider = portal_provider

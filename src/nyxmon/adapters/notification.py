import os
import httpx
import logging

from typing import Protocol
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

    def set_portal_provider(self, portal_provider: BlockingPortalProvider) -> None:
        """Set the portal provider for async operations."""
        self.portal_provider = portal_provider

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

        # Escape all text for MarkdownV2
        escaped_name = (
            self.escape_markdown_v2(check.name) if check.name else "Unnamed Check"
        )
        escaped_url = self.escape_markdown_v2(check.url)
        escaped_error_msg = self.escape_markdown_v2(str(error_msg))
        escaped_error_type = self.escape_markdown_v2(str(error_type))

        message = "🔴 *Check Failed*\n"
        message += f"Name: {escaped_name}\n"
        message += f"URL: {escaped_url}\n"
        if status_code:
            message += f"Status: {status_code}\n"
        if error_type:
            message += f"Error Type: {escaped_error_type}\n"
        message += f"Error: {escaped_error_msg}"

        await self.async_send(message, high_priority=True)

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

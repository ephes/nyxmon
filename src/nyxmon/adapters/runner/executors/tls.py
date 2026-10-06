"""Shared TLS helpers for check executors."""

from __future__ import annotations

import logging
import ssl

logger = logging.getLogger(__name__)


def build_client_ssl_context(
    verify: bool, *, check_type: str, host: str
) -> ssl.SSLContext:
    """Return a client SSL context for a mail check connection.

    With ``verify`` set (the default for every check), the context requires a
    valid certificate chain and a matching hostname. Passing no context to
    ``imaplib``/``smtplib`` would silently fall back to an unverified context,
    so executors must always hand over the result of this function.

    Args:
        verify: Whether to verify the server certificate and hostname.
        check_type: Check type used in the warning when verification is off.
        host: Target host used in the warning when verification is off.

    Returns:
        A verifying context, or an explicitly unverified one if ``verify`` is
        False.
    """
    if verify:
        return ssl.create_default_context()

    logger.warning(
        "%s check for %s runs with TLS certificate verification disabled "
        "(verify=false); credentials are exposed to man-in-the-middle attacks",
        check_type,
        host,
    )
    context = ssl._create_unverified_context()  # type: ignore[attr-defined]
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context

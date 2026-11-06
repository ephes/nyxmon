"""Shared bounds for timestamps read from worker-owned state."""

from datetime import datetime, timezone

# Last whole second representable by a UTC datetime, independent of libc.
MAX_EPOCH = 253_402_300_799


def usable_epoch(value):
    """Return ``value`` as a positive integer epoch, or ``None``.

    Args:
        value: Anything a payload might carry in a timestamp field.

    Returns:
        The epoch as an ``int`` when it is a usable positive timestamp,
        otherwise ``None``. Booleans, strings, ``None``, ``nan`` and
        out-of-range numbers all yield ``None`` rather than raising: the
        payload is written by the worker and must never be able to break the
        dashboard or digest.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        epoch = int(value)
        if not 0 < epoch <= MAX_EPOCH:
            return None
        datetime.fromtimestamp(epoch, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return epoch

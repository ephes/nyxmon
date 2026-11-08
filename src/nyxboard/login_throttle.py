"""Limit failed logins through Django authentication, including the admin."""

from __future__ import annotations

import hashlib
import ipaddress
import time

from asgiref.sync import sync_to_async

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.core.exceptions import PermissionDenied
from django.db import router, transaction
from django.db.models import F
from django.db.models.expressions import CombinedExpression

from .models import LoginThrottleBucket

MAX_USERNAME = 150  # longer names are cut for the key; they cannot belong to an account


def client_address(request) -> str:
    """The address failures are counted for.

    ``NYXBOARD_LOGIN_PROXY_HOPS`` says how many reverse proxies sit in front of the app. With ``n`` hops the
    n-th entry from the right of ``X-Forwarded-For`` is the address the outermost trusted proxy saw; anything
    left of it was sent by the client and is ignored. With 0 hops, or when the header is missing, too short or
    not an address, the socket address is used."""
    remote = request.META.get("REMOTE_ADDR", "") or "unknown"
    hops = settings.NYXBOARD_LOGIN_PROXY_HOPS
    if hops <= 0:
        return remote
    parts = [
        part.strip()
        for part in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")
        if part.strip()
    ]
    if len(parts) < hops:
        return remote
    candidate = parts[-hops]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return remote


def _keys(request, username: str) -> tuple[str, str]:
    address = client_address(request)
    name = (username or "")[:MAX_USERNAME].casefold()
    pair = hashlib.sha256(f"{name}\x00{address}".encode()).hexdigest()
    return (
        f"nyxboard:login:pair:{pair}",
        f"nyxboard:login:addr:{hashlib.sha256(address.encode()).hexdigest()}",
    )


def _reserve(request, username: str) -> tuple[str, tuple[tuple[str, float], ...]]:
    """Atomically reserve both budgets, without holding locks during hashing."""
    alias = router.db_for_write(LoginThrottleBucket)
    buckets = LoginThrottleBucket.objects.using(alias)
    now = time.time()
    limits = dict(
        zip(
            _keys(request, username),
            (
                settings.NYXBOARD_LOGIN_FAILURE_LIMIT,
                settings.NYXBOARD_LOGIN_ADDRESS_FAILURE_LIMIT,
            ),
        )
    )
    owned = []
    with transaction.atomic(using=alias):
        # The first statement is a write, including on SQLite (where row locks
        # do not exist). Bound expiry cleanup and acquire the writer before reads.
        expired = buckets.filter(expires_at__lte=now).values("key")[:100]
        buckets.filter(key__in=expired).delete()
        # Address first: a locked address cannot create arbitrary username rows.
        for key in sorted(limits):
            buckets.filter(key=key, expires_at__lte=now).delete()
            bucket, _ = buckets.get_or_create(
                key=key,
                defaults={
                    "expires_at": now + settings.NYXBOARD_LOGIN_FAILURE_WINDOW_SECONDS
                },
            )
            admitted = (
                buckets.filter(key=key)
                .annotate(attempts=F("failures") + F("pending"))
                .filter(attempts__lt=limits[key])
                .update(pending=F("pending") + 1)
            )
            if not admitted:
                # Rolls back every earlier reservation in this transaction.
                raise PermissionDenied("Too many failed login attempts.")
            owned.append((key, bucket.expires_at))
    return alias, tuple(owned)


def _settle(reservation, pair: str, *, success: bool, failed: bool) -> None:
    alias, owned = reservation
    with transaction.atomic(using=alias):
        for key, expires_at in owned:
            values: dict[str, int | CombinedExpression] = {"pending": F("pending") - 1}
            if failed:
                values["failures"] = F("failures") + 1
            elif success and key == pair:
                values["failures"] = 0
            # An old completion must not debit a replacement window.
            LoginThrottleBucket.objects.using(alias).filter(
                key=key, expires_at=expires_at, pending__gt=0
            ).update(**values)


class LoginThrottleBackend(ModelBackend):
    """Own admission, password verification and settlement for both login views.

    Keep ModelBackend configured after this backend for existing sessions; a
    failed verification raises PermissionDenied to prevent a second password
    check through that compatibility backend.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None:
            username = kwargs.get(get_user_model().USERNAME_FIELD)
        reservation = None
        if (
            request is not None
            and isinstance(username, str)
            and settings.NYXBOARD_LOGIN_FAILURE_LIMIT > 0
        ):
            reservation = _reserve(request, username)
        user = None
        completed = False
        try:
            user = super().authenticate(request, username, password, **kwargs)
            completed = True
        finally:
            if reservation is not None:
                _settle(
                    reservation,
                    _keys(request, username)[0],
                    success=user is not None,
                    failed=completed and user is None,
                )
        if user is None:
            raise PermissionDenied("Invalid login.")
        return user

    async def aauthenticate(self, request, username=None, password=None, **kwargs):
        # Do not inherit ModelBackend's unthrottled asynchronous implementation.
        return await sync_to_async(self.authenticate)(
            request, username, password, **kwargs
        )

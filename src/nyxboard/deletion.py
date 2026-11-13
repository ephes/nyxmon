"""Confirm destructive actions against definitions, independent of runtime data."""

import json

from django.db import transaction
from django.db.models import F
from django.shortcuts import get_object_or_404
from django.utils.crypto import constant_time_compare, salted_hmac

from .forms import DEFINITION_FIELDS
from .models import Service

DELETE_CONFLICT = (
    "This definition or its health checks changed. Please reload and review "
    "the current definition before confirming deletion."
)


def _check_definition(check):
    return [check.pk, {name: getattr(check, name) for name in DEFINITION_FIELDS}]


def deletion_revision(instance, *, children=None):
    """Opaque HMAC: service name and child definitions, or one check definition."""
    if isinstance(instance, Service):
        if children is None:
            children = instance.healthcheck_set.order_by("pk")
        definition = [
            "service",
            instance.pk,
            instance.name,
            [_check_definition(check) for check in children],
        ]
    else:
        definition = ["healthcheck", _check_definition(instance)]
    return salted_hmac(
        "nyxboard.delete-definition",
        json.dumps(definition, sort_keys=True),
        algorithm="sha256",
    ).hexdigest()


def delete_confirmed(model, pk, submitted_revision):
    """Reserve the write boundary before reading, then validate and cascade.

    The no-op write reserves SQLite's writer before any definition/child-set
    read. Row locks also serialize the definition and children on row-locking
    backends. No probe or network operation runs inside this short transaction.
    """
    with transaction.atomic(using=model.objects.db):
        model.objects.filter(pk=pk).update(name=F("name"))
        instance = get_object_or_404(model.objects.select_for_update(), pk=pk)
        children = None
        if isinstance(instance, Service):
            children = list(instance.healthcheck_set.select_for_update().order_by("pk"))
        current = deletion_revision(instance, children=children)
        if not submitted_revision or not constant_time_compare(
            submitted_revision, current
        ):
            return instance, False
        instance.delete()
        return instance, True

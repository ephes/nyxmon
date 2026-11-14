"""Opaque check revisions for card actions."""

from django import template

from nyxboard.forms import check_definition_revision

register = template.Library()
register.simple_tag(check_definition_revision)

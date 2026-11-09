#!/usr/bin/env python3
"""Placeholder (0.6.0 scaffold): `pk research` and the `research` section of checks.yaml."""
from __future__ import annotations

from common import KitError


def parse(value) -> dict | None:
    """The `research` section of checks.yaml, or None when absent."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise KitError("checks.yaml: research must be a mapping")
    return dict(value)


def register(add_command) -> None:
    """No command yet."""

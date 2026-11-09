"""Schema for broilking.discover_commands.

Home Assistant 2026.11 aliases ``voluptuous`` to ``probatio._vol_shim``.
That module has ``PREVENT_EXTRA`` and does not have ``PREVENT``.
"""
from __future__ import annotations

import voluptuous as vol

DISCOVER_SCHEMA = vol.Schema(
    {vol.Optional("entry_id"): str},
    extra=vol.PREVENT_EXTRA,
)

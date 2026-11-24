# SPDX-License-Identifier: Apache-2.0
"""Shared helper: submit a form the way the browser does."""

from __future__ import annotations

from typing import Any

import voluptuous as vol


def prefill_submission(schema: vol.Schema) -> dict[str, Any]:
    """What the frontend submits when the user touches nothing.

    Each field arrives pre-filled with its schema default or, failing that, its
    suggested value, and the browser sends that prefill back. A field the user
    clears is the one key missing from the submission, so a caller that wants
    "cleared" removes the key; a bare `{}` would mean every field was cleared.
    """
    out: dict[str, Any] = {}
    for key in schema.schema:
        suggested = (getattr(key, "description", None) or {}).get("suggested_value")
        if suggested is not None:
            out[str(key)] = suggested
        elif key.default is not vol.UNDEFINED:
            out[str(key)] = key.default()
    return out

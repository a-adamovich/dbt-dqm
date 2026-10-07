"""Turn background-job failures into short reviewer-facing messages.

The UI shows `AppError.message`; `AppError.details` keeps the technical text (dbt output, the
original exception) for the "Technical details" expander and the app log.
"""

from __future__ import annotations

import subprocess
import traceback

from google.api_core.exceptions import GoogleAPICallError
from psycopg2 import errors as pg_errors


class AppError(Exception):
    def __init__(self, message: str, details: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class SnapshotUnavailable(AppError):
    """The warehouse answered, but its data can't be trusted to replace the local cache."""


class WarehouseBusy(AppError):
    """A reconciliation or another writer held the data; nothing was changed."""


BUSY_MESSAGE = (
    "A DQM reconciliation is running, so the warehouse didn't accept the change. "
    "Your edits are kept locally; retry in a moment."
)

# Ordered: the first matching hint wins. Matched case-insensitively against dbt's output.
_DBT_HINTS = (
    (
        ("could not find profile", "profiles.yml", "does not have a target named"),
        "dbt couldn't find the selected profile or target. Check --profiles-dir and --target.",
    ),
    (
        (
            "password authentication failed",
            "fe_sendauth",
            "invalid_grant",
            "could not automatically determine credentials",
            "permission denied",
            "access denied",
            "403 forbidden",
        ),
        "dbt couldn't sign in to the warehouse with this profile. Check its credentials.",
    ),
    (
        ("connection refused", "could not connect", "timed out", "name or service not known"),
        "dbt couldn't reach the warehouse. Check the network and the profile's host settings.",
    ),
    (
        ("parsing error", "compilation error"),
        "dbt couldn't parse the project. Run `dbt parse` in the project to see the problem.",
    ),
)

_BIGQUERY_CONFLICT_MARKERS = (
    "concurrent update",
    "transaction is aborted",
    "could not serialize access",
    "another dqm reconciliation is active",
)


def classify(error: BaseException) -> AppError:
    """Map a worker exception to an AppError with a concise message."""
    if isinstance(error, AppError):
        return error
    details = "".join(traceback.format_exception(error)).strip()
    if isinstance(error, subprocess.CalledProcessError):
        output = f"{error.stdout or ''}\n{error.stderr or ''}".strip()
        command = " ".join(str(part) for part in error.cmd[:2]) if error.cmd else "dbt"
        lowered = output.lower()
        for markers, message in _DBT_HINTS:
            if any(marker in lowered for marker in markers):
                return AppError(message, output or details)
        return AppError(f"`{command}` failed.", output or details)
    if isinstance(error, pg_errors.LockNotAvailable):
        return WarehouseBusy(BUSY_MESSAGE, details)
    if isinstance(error, GoogleAPICallError) and any(
        marker in str(error).lower() for marker in _BIGQUERY_CONFLICT_MARKERS
    ):
        return WarehouseBusy(BUSY_MESSAGE, details)
    return AppError(f"The background job failed: {error}", details)

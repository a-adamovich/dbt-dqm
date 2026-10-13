"""Turn background-job failures into short reviewer-facing messages.

The UI shows `AppError.message`; `AppError.details` keeps the technical text (dbt output, the
original exception) for the "Technical details" expander and the app log.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import traceback


class AppError(Exception):
    def __init__(self, message: str, details: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class SnapshotUnavailable(AppError):
    """The warehouse answered, but its data can't be trusted to replace the local cache."""


class SearchTooCostly(AppError):
    """A BigQuery search would process more bytes than --max-search-gib allows; nothing ran."""


class WarehouseBusy(AppError):
    """A reconciliation or another writer held the data; nothing was changed."""


BUSY_MESSAGE = (
    "A DQM reconciliation is running, so the warehouse didn't accept the change. "
    "Your edits are kept locally; retry in a moment."
)

# Ordered: the first matching hint wins. Matched case-insensitively against dbt's output.
_DBT_HINTS = (
    (
        # Only the failure forms: dbt debug also prints "profiles.yml file [OK found and valid]".
        ("could not find profile", "does not have a target named", "profiles.yml file [error"),
        "dbt couldn't find the selected profile or target. Check --profiles-dir and --target.",
    ),
    (
        ("this version of dbt is not supported", "dbt_project.yml file [error"),
        (
            "The dbt project doesn't support the installed dbt version, or its dbt_project.yml "
            "is invalid. Run `dbt debug` in the project for details."
        ),
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

# dbt colours its status markers ("[\x1b[31mERROR invalid\x1b[0m]"); match on the plain text.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

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
        full_command = (subprocess.list2cmdline(error.cmd) if os.name == "nt"
                        else shlex.join(str(part) for part in error.cmd))
        details = (f"Command: {full_command}\nExecutable: {error.cmd[0]}\n"
                   f"Exit code: {error.returncode}\nStdout:\n{error.stdout or ''}\n"
                   f"Stderr:\n{error.stderr or ''}")
        lowered = _ANSI.sub("", output).lower()
        for markers, message in _DBT_HINTS:
            if any(marker in lowered for marker in markers):
                return AppError(message, details)
        return AppError(f"`{command}` failed.", details)
    modules = {cls.__module__ for cls in type(error).__mro__}
    if any(module.startswith("psycopg2") for module in modules):
        from psycopg2 import errors as pg_errors

        if isinstance(error, pg_errors.LockNotAvailable):
            return WarehouseBusy(BUSY_MESSAGE, details)
    if "google.api_core.exceptions" in modules:
        from google.api_core.exceptions import GoogleAPICallError

        if isinstance(error, GoogleAPICallError) and any(
            marker in str(error).lower() for marker in _BIGQUERY_CONFLICT_MARKERS
        ):
            return WarehouseBusy(BUSY_MESSAGE, details)
    return AppError(f"The background job failed: {error}", details)

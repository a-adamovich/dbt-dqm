from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import date, datetime
from html import escape
from typing import Any

WORKFLOW_STATUSES = (
    "NEW",
    "TRIAGED",
    "IN_PROGRESS",
    "BLOCKED",
    "RESOLVED",
    "ACCEPTED_RISK",
    "FALSE_POSITIVE",
)

WORKFLOW_STATUS_HELP = """Choose the review state for this issue.

- **NEW** — not yet reviewed.
- **TRIAGED** — reviewed and categorized; next action is known.
- **IN_PROGRESS** — someone is actively investigating or fixing it.
- **BLOCKED** — progress depends on another person, system, or decision.
- **RESOLVED** — the remediation is complete; the next passing dbt test should archive it.
- **ACCEPTED_RISK** — known and intentionally tolerated for now.
- **FALSE_POSITIVE** — not a real data-quality issue; improve or remove the test when practical.
"""

OWNER_UNASSIGNED_OPTION = "— Unassigned —"


def _render_value(value: Any, is_null: bool) -> str:
    if is_null:
        return "NULL"
    # JSON quoting makes empty strings, whitespace, delimiters, quotes, and newlines explicit.
    return json.dumps("" if value is None else str(value), ensure_ascii=False)


def paired_display(
    structured_json: Any,
) -> str:
    """Format stored column/value pairs as one unambiguous line per column.

    Stored values are a portable JSON object whose property names are column names.
    """
    return "\n".join(
        f"{column_name} = {rendered_value}"
        for column_name, rendered_value in column_value_pairs(structured_json)
    )


def column_value_pairs(
    structured_json: Any,
) -> list[tuple[str, str]]:
    """Return ordered, display-safe column/value pairs from the JSON record representation."""
    if structured_json is None or (
        isinstance(structured_json, float) and math.isnan(structured_json)
    ):
        return []
    if isinstance(structured_json, Mapping):
        pairs = structured_json
    else:
        try:
            pairs = json.loads(str(structured_json))
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
    if isinstance(pairs, dict):
        return [
            (str(column_name), _render_value(value, value is None))
            for column_name, value in pairs.items()
        ]

    return []


def workflow_status_options(current_value: Any) -> list[str]:
    """Return controlled statuses while retaining any value written by an older version."""
    current = "NEW" if current_value is None else str(current_value).strip().upper()
    options = list(WORKFLOW_STATUSES)
    if current and current not in options:
        options.append(current)
    return options


def workflow_status_help() -> str:
    """Return the shared tooltip copy used beside each status selector."""
    return WORKFLOW_STATUS_HELP


def owner_label(value: Any) -> str:
    """Render the optional annotation owner consistently for cards and filters."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "Unassigned"
    owner = str(value).strip()
    return owner or "Unassigned"


def owner_editor_options(values: Any) -> list[str]:
    """Return distinct existing owners plus an explicit choice that clears ownership."""
    owners = {
        owner
        for value in values
        if (owner := owner_label(value)) != "Unassigned"
    }
    return [OWNER_UNASSIGNED_OPTION, *sorted(owners, key=lambda owner: (owner.casefold(), owner))]


def owner_value_from_selection(value: Any) -> str | None:
    """Convert the editor's unassigned choice to the nullable warehouse annotation."""
    if value is None or value == OWNER_UNASSIGNED_OPTION or (
        isinstance(value, float) and math.isnan(value)
    ):
        return None
    normalized = str(value).strip()
    return normalized or None


def format_timestamp(value: Any, empty_label: str = "—") -> str:
    """Render warehouse timestamps in the workstation's local timezone."""
    if value is None or str(value).strip() in {"", "NaT", "None"}:
        return empty_label
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        timezone = parsed.tzname() or ""
        return f"{parsed:%Y-%m-%d %H:%M} {timezone}".strip()
    except ValueError:
        return str(value)


def calendar_date(value: Any) -> date | None:
    """Return an issue timestamp's local calendar date for inclusive review filters."""
    if value is None or str(value).strip() in {"", "NaT", "None"}:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        return parsed.date()
    except ValueError:
        return None


def record_fields_html(
    pairs: list[tuple[str, str]],
    empty_message: str = "Record fields unavailable — synchronize or rebuild dbt-dqm.",
) -> str:
    """Build a safe two-column record table with one visibly separated row per field."""
    rows = "".join(
        '<div class="dqm-field-row">'
        f'<div class="dqm-field-name">{escape(name)}</div>'
        f'<div class="dqm-field-value">{escape(value)}</div>'
        "</div>"
        for name, value in pairs
    )
    return f"""
    <style>
      .dqm-fields {{
        border: 1px solid rgba(128, 128, 128, 0.35);
        border-radius: 0.5rem;
        overflow: hidden;
        margin-bottom: 0.75rem;
      }}
      .dqm-field-row {{
        display: grid;
        grid-template-columns: minmax(10rem, 28%) minmax(0, 72%);
        border-bottom: 1px solid rgba(128, 128, 128, 0.25);
      }}
      .dqm-field-row:last-child {{ border-bottom: 0; }}
      .dqm-field-name, .dqm-field-value {{
        padding: 0.55rem 0.75rem;
        white-space: pre-wrap;
        overflow-wrap: anywhere;
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      }}
      .dqm-field-name {{
        font-weight: 650;
        background: rgba(128, 128, 128, 0.10);
        border-right: 1px solid rgba(128, 128, 128, 0.25);
      }}
    </style>
    <div class="dqm-fields">
      <div class="dqm-field-row">
        <div class="dqm-field-name">Column</div>
        <div class="dqm-field-value"><strong>Value</strong></div>
      </div>
      {rows or f'<div class="dqm-field-row"><div class="dqm-field-name">⚠</div><div class="dqm-field-value">{escape(empty_message)}</div></div>'}
    </div>
    """

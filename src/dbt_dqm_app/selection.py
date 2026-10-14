"""Bounded, session-local saved baselines for explicit cross-page selections."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .errors import AppError
from .limits import issue_json

MAX_SELECTED_ISSUES = 2_000
MAX_SELECTED_BYTES = 32 * 2**20


@dataclass
class Selection:
    rows: dict[str, str] = field(default_factory=dict)

    def add(self, rows) -> None:
        additions = {str(row["occurrence_id"]): issue_json(row) for row in rows}
        proposed = additions | self.rows
        if (len(proposed) > MAX_SELECTED_ISSUES or
                sum(len(value.encode("utf-8")) for value in proposed.values()) > MAX_SELECTED_BYTES):
            raise AppError("This selection is too large. Stage the current batch before "
                           "selecting more issues (up to 2,000 issues or 32 MiB).")
        # Re-selecting never replaces the original saved version.
        for identity, value in additions.items():
            self.rows.setdefault(identity, value)

    def remove(self, identity) -> None:
        self.rows.pop(str(identity), None)

    def baselines(self) -> list[dict]:
        return [json.loads(value) for value in self.rows.values()]

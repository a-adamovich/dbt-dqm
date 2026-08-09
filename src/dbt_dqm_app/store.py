from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

EDITABLE_FIELDS = (
    "test_status",
    "call_to_action",
    "ticket_url",
    "notes",
    "poc_responsible",
)


@dataclass(frozen=True)
class Patch:
    occurrence_id: str
    field_name: str
    old_value: str | None
    new_value: str | None
    version: int
    changed_at: str


class Workspace:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                pragma journal_mode = wal;
                create table if not exists snapshot (
                  occurrence_id text primary key,
                  payload_json text not null,
                  synced_at text not null
                );
                create table if not exists pending_changes (
                  occurrence_id text not null,
                  field_name text not null,
                  old_value text,
                  new_value text,
                  version integer not null,
                  changed_at text not null,
                  primary key (occurrence_id, field_name)
                );
                create table if not exists metadata (
                  key text primary key,
                  value text
                );
                """
            )

    def replace_snapshot(self, rows: Iterable[dict[str, Any]]) -> None:
        now = datetime.now(UTC).isoformat()
        records = [
            (str(row["occurrence_id"]), json.dumps(row, default=str), now) for row in rows
        ]
        with self.connect() as connection:
            connection.execute("delete from snapshot")
            connection.executemany(
                "insert into snapshot(occurrence_id, payload_json, synced_at) values (?, ?, ?)",
                records,
            )
            connection.execute(
                "insert into metadata(key, value) values ('last_sync', ?) "
                "on conflict(key) do update set value=excluded.value",
                (now,),
            )

    def rows(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            snapshots = {
                row["occurrence_id"]: json.loads(row["payload_json"])
                for row in connection.execute("select * from snapshot")
            }
            for patch in connection.execute("select * from pending_changes"):
                if patch["occurrence_id"] in snapshots:
                    snapshots[patch["occurrence_id"]][patch["field_name"]] = patch["new_value"]
                    snapshots[patch["occurrence_id"]]["_dirty"] = True
        return list(snapshots.values())

    def set_change(self, occurrence_id: str, field_name: str, new_value: Any) -> None:
        if field_name not in EDITABLE_FIELDS:
            raise ValueError(f"Field is not editable: {field_name}")
        normalized = None if new_value is None else str(new_value)
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            row = connection.execute(
                "select payload_json from snapshot where occurrence_id=?", (occurrence_id,)
            ).fetchone()
            if row is None:
                raise KeyError(occurrence_id)
            base = json.loads(row["payload_json"]).get(field_name)
            base = None if base is None else str(base)
            if normalized == base:
                connection.execute(
                    "delete from pending_changes where occurrence_id=? and field_name=?",
                    (occurrence_id, field_name),
                )
                return
            current = connection.execute(
                "select version from pending_changes where occurrence_id=? and field_name=?",
                (occurrence_id, field_name),
            ).fetchone()
            version = (current["version"] if current else 0) + 1
            connection.execute(
                """
                insert into pending_changes
                  (occurrence_id, field_name, old_value, new_value, version, changed_at)
                values (?, ?, ?, ?, ?, ?)
                on conflict(occurrence_id, field_name) do update set
                  new_value=excluded.new_value,
                  version=excluded.version,
                  changed_at=excluded.changed_at
                """,
                (occurrence_id, field_name, base, normalized, version, now),
            )

    def pending(self) -> list[Patch]:
        with self.connect() as connection:
            return [Patch(**dict(row)) for row in connection.execute(
                "select * from pending_changes order by changed_at"
            )]

    def clear_applied(self, patches: Iterable[Patch]) -> None:
        with self.connect() as connection:
            connection.executemany(
                "delete from pending_changes where occurrence_id=? and field_name=? and version=?",
                [(p.occurrence_id, p.field_name, p.version) for p in patches],
            )

    def last_sync(self) -> str | None:
        with self.connect() as connection:
            row = connection.execute("select value from metadata where key='last_sync'").fetchone()
            return row["value"] if row else None

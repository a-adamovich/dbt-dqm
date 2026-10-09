from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .limits import MAX_CACHE_ISSUES, MAX_CACHE_MIB, check_cache_size, issue_json

EDITABLE_FIELDS = (
    "workflow_status",
    "review_verdict",
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
    base_annotation_version: int = 0


class Workspace:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        self._initialize()
        self._secure_files()

    def _secure_files(self) -> None:
        for path in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
            if path.exists():
                path.chmod(0o600)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        self._secure_files()
        return connection

    def _connect_immediate(self) -> sqlite3.Connection:
        """A connection in manual-transaction (autocommit) mode, for callers that need to open
        the transaction themselves with `BEGIN IMMEDIATE` before their first read. Python's
        sqlite3 module only auto-opens a transaction ahead of a write statement, which is too
        late for a read-modify-write sequence such as set_change's version counter: two
        connections could both read the same "current" version before either has written,
        and the second write would silently lose the first's increment."""
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        self._secure_files()
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
                  base_annotation_version integer not null default 0,
                  primary key (occurrence_id, field_name)
                );
                create table if not exists health_snapshot (
                  kind text primary key, payload_json text not null, synced_at text not null
                );
                create table if not exists metadata (
                  key text primary key,
                  value text
                );
                """
            )
            columns = {
                row["name"] for row in connection.execute("pragma table_info(pending_changes)")
            }
            if "base_annotation_version" not in columns:
                connection.execute(
                    "alter table pending_changes add column "
                    "base_annotation_version integer not null default 0"
                )
            connection.execute(
                "update pending_changes set field_name='workflow_status' "
                "where field_name='test_status'"
            )

    def _replace_snapshot(self, connection, rows, now, max_issues, max_bytes) -> None:
        # Stage in SQLite instead of duplicating the entire incoming Python snapshot. The
        # transaction also fences pending edits while deciding which older issues to retain.
        connection.execute("create temp table incoming as select * from snapshot where false")
        connection.execute("create unique index incoming_id on incoming(occurrence_id)")
        size = 0
        for count, row in enumerate(rows, start=1):
            payload = issue_json(row)
            size += len(payload.encode("utf-8"))
            check_cache_size(count, size, max_issues, max_bytes)
            connection.execute("insert into incoming values (?,?,?)",
                               (str(row["occurrence_id"]), payload, now))
        connection.execute(
            "insert into incoming select s.occurrence_id, "
            "json_set(s.payload_json, '$._outside_cache', json('true')), ? from snapshot s "
            "where exists(select 1 from pending_changes p where p.occurrence_id=s.occurrence_id) "
            "and not exists(select 1 from incoming i where i.occurrence_id=s.occurrence_id)", (now,)
        )
        count, size = connection.execute(
            "select count(*), coalesce(sum(length(cast(payload_json as blob))),0) from incoming"
        ).fetchone()
        # Pending text will be overlaid when rendering; account for it before materializing
        # either the incoming or historical snapshot. Conservatively charge the new text even
        # when it replaces a similarly sized old value.
        size += connection.execute(
            "select coalesce(sum(length(cast(coalesce(p.new_value,'') as blob))),0) "
            "from pending_changes p join incoming i using(occurrence_id)"
        ).fetchone()[0]
        check_cache_size(count, size, max_issues, max_bytes)
        connection.execute("delete from snapshot")
        connection.execute("insert into snapshot select * from incoming")
        connection.execute(
            "insert into metadata(key,value) values ('last_sync',?) "
            "on conflict(key) do update set value=excluded.value", (now,)
        )

    def replace_snapshot(self, rows: Iterable[dict[str, Any]], *,
                         max_issues=MAX_CACHE_ISSUES, max_bytes=MAX_CACHE_MIB * 2**20) -> None:
        self.replace_synced_data(rows, None, max_issues=max_issues, max_bytes=max_bytes)

    def replace_synced_data(self, rows, health, *,
                            max_issues=MAX_CACHE_ISSUES, max_bytes=MAX_CACHE_MIB * 2**20) -> None:
        now = datetime.now(UTC).isoformat()
        connection = self._connect_immediate()
        try:
            connection.execute("begin immediate")
            self._replace_snapshot(connection, rows, now, max_issues, max_bytes)
            if health is not None:
                connection.execute("delete from health_snapshot")
                connection.executemany(
                    "insert into health_snapshot(kind,payload_json,synced_at) values (?,?,?)",
                    ((kind, json.dumps(value, default=str), now) for kind, value in health.items()),
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def replace_health(self, health: dict[str, Any]) -> None:
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            connection.execute("delete from health_snapshot")
            connection.executemany(
                "insert into health_snapshot(kind,payload_json,synced_at) values (?,?,?)",
                [(kind, json.dumps(value, default=str), now) for kind, value in health.items()],
            )

    def health_rows(self) -> dict[str, Any]:
        with self.connect() as connection:
            return {
                row["kind"]: json.loads(row["payload_json"])
                for row in connection.execute("select * from health_snapshot")
            }

    def cache_size(self) -> tuple[int, int]:
        with self.connect() as connection:
            return self._cache_size(connection)

    @staticmethod
    def _cache_size(connection) -> tuple[int, int]:
        count, size = connection.execute(
            "select count(*), coalesce(sum(length(cast(payload_json as blob))),0) from snapshot"
        ).fetchone()
        size += connection.execute(
            "select coalesce(sum(length(cast(coalesce(p.new_value,'') as blob))),0) "
            "from pending_changes p join snapshot s using(occurrence_id)"
        ).fetchone()[0]
        return count, size

    def rows(self, *, max_issues=MAX_CACHE_ISSUES,
             max_bytes=MAX_CACHE_MIB * 2**20) -> list[dict[str, Any]]:
        with self.connect() as connection:
            count, size = self._cache_size(connection)
            check_cache_size(count, size, max_issues, max_bytes)
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
        if field_name == "review_verdict" and normalized not in {
            "UNREVIEWED",
            "TRUE_POSITIVE",
            "FALSE_POSITIVE",
        }:
            raise ValueError("Invalid review verdict")
        now = datetime.now(UTC).isoformat()
        connection = self._connect_immediate()
        try:
            connection.execute("begin immediate")
            row = connection.execute(
                "select json_extract(payload_json, ?) as value, "
                "json_extract(payload_json, '$.annotation_version') as annotation_version "
                "from snapshot where occurrence_id=?", ("$." + field_name, occurrence_id)
            ).fetchone()
            if row is None:
                raise KeyError(occurrence_id)
            base = row["value"]
            base = None if base is None else str(base)
            base_annotation_version = int(row["annotation_version"] or 0)
            if normalized == base:
                connection.execute(
                    "delete from pending_changes where occurrence_id=? and field_name=?",
                    (occurrence_id, field_name),
                )
                connection.commit()
                return
            current = connection.execute(
                "select version from pending_changes where occurrence_id=? and field_name=?",
                (occurrence_id, field_name),
            ).fetchone()
            version = (current["version"] if current else 0) + 1
            connection.execute(
                """
                insert into pending_changes
                  (occurrence_id, field_name, old_value, new_value, version, changed_at,
                   base_annotation_version)
                values (?, ?, ?, ?, ?, ?, ?)
                on conflict(occurrence_id, field_name) do update set
                  new_value=excluded.new_value,
                  version=excluded.version,
                  changed_at=excluded.changed_at
                """,
                (
                    occurrence_id,
                    field_name,
                    base,
                    normalized,
                    version,
                    now,
                    base_annotation_version,
                ),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def drifted_patches(self) -> list[Patch]:
        """Pending patches whose recorded base (`old_value`) no longer matches the current
        synced snapshot for that field — meaning the warehouse value changed since the patch
        was staged. Applying now would use local-wins semantics and silently overwrite that
        newer remote value; callers should surface this instead of applying quietly."""
        with self.connect() as connection:
            drifted = []
            for patch_row in connection.execute(
                "select p.*, json_extract(s.payload_json, '$.' || p.field_name) as current_value, "
                "json_extract(s.payload_json, '$.annotation_version') as current_version "
                "from pending_changes p join snapshot s using(occurrence_id)"
            ):
                values = dict(patch_row)
                current = values.pop("current_value")
                current_annotation_version = int(values.pop("current_version") or 0)
                patch = Patch(**values)
                current = None if current is None else str(current)
                if (
                    current != patch.old_value
                    or current_annotation_version != patch.base_annotation_version
                ):
                    drifted.append(patch)
        return drifted

    def pending_count(self) -> int:
        with self.connect() as connection:
            return connection.execute("select count(*) from pending_changes").fetchone()[0]

    def pending_issue_count(self) -> int:
        with self.connect() as connection:
            return connection.execute(
                "select count(distinct occurrence_id) from pending_changes"
            ).fetchone()[0]

    def pending_page(self, page=1, max_bytes=MAX_CACHE_MIB * 2**20) -> list[Patch]:
        if page < 1:
            raise ValueError("Pending page must be positive")
        with self.connect() as connection:
            selection = (
                "select occurrence_id from pending_changes group by occurrence_id "
                "order by occurrence_id limit 100 offset ?"
            )
            offset = (page - 1) * 100
            size = connection.execute(
                "select coalesce(sum(length(cast(coalesce(old_value,'') as blob)) + "
                "length(cast(coalesce(new_value,'') as blob))),0) from pending_changes "
                f"where occurrence_id in ({selection})", (offset,)
            ).fetchone()[0]
            check_cache_size(0, size, MAX_CACHE_ISSUES, max_bytes)
            return [Patch(**dict(row)) for row in connection.execute(
                f"select * from pending_changes where occurrence_id in ({selection}) "
                "order by occurrence_id, field_name", (offset,)
            )]

    def pending_previews(self, page=1) -> list[Patch]:
        """Truncated display/discard tokens; never pass these previews to Apply."""
        with self.connect() as connection:
            return [Patch(**dict(row)) for row in connection.execute(
                "select occurrence_id,field_name,substr(old_value,1,4000) as old_value, "
                "substr(new_value,1,4000) as new_value,version,changed_at,base_annotation_version "
                "from pending_changes where occurrence_id in (select occurrence_id "
                "from pending_changes group by occurrence_id order by occurrence_id "
                "limit 100 offset ?) order by occurrence_id,field_name", ((page - 1) * 100,)
            )]

    def pending(self) -> list[Patch]:
        with self.connect() as connection:
            return [
                Patch(**dict(row))
                for row in connection.execute("select * from pending_changes order by changed_at")
            ]

    def clear_applied(self, patches: Iterable[Patch]) -> None:
        with self.connect() as connection:
            connection.executemany(
                "delete from pending_changes where occurrence_id=? and field_name=? and version=?",
                [(p.occurrence_id, p.field_name, p.version) for p in patches],
            )

    def discard_patches(self, patches: Iterable[Patch] | None = None) -> None:
        with self.connect() as connection:
            if patches is None:
                connection.execute("delete from pending_changes")
            else:
                connection.executemany(
                    "delete from pending_changes where occurrence_id=? and field_name=? and version=?",
                    [(p.occurrence_id, p.field_name, p.version) for p in patches],
                )

    def rebase_patches(self, patches: Iterable[Patch]) -> None:
        """Explicit local-wins choice: move selected patches onto the latest synced version."""
        with self.connect() as connection:
            for patch in patches:
                row = connection.execute(
                    "select payload_json from snapshot where occurrence_id=?",
                    (patch.occurrence_id,),
                ).fetchone()
                if row is None:
                    continue
                snapshot = json.loads(row["payload_json"])
                current = snapshot.get(patch.field_name)
                current = None if current is None else str(current)
                connection.execute(
                    "update pending_changes set old_value=?, base_annotation_version=? "
                    "where occurrence_id=? and field_name=? and version=?",
                    (
                        current,
                        int(snapshot.get("annotation_version") or 0),
                        patch.occurrence_id,
                        patch.field_name,
                        patch.version,
                    ),
                )

    def last_sync(self) -> str | None:
        with self.connect() as connection:
            row = connection.execute("select value from metadata where key='last_sync'").fetchone()
            return row["value"] if row else None

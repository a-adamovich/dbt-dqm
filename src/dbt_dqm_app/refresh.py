"""Explicit, narrowly scoped schema reset. No history migration or cascading deletion."""
from __future__ import annotations

import json
import logging
import uuid

from . import warehouse

CONTRACT = "dqm-full-v1"


def confirm_refresh(config, manifest, workflow) -> bool:
    """Describe the actual destinations before any reset preparation or warehouse writes."""
    destination = workflow.settings["refresh"]["dqm_install"]
    print(f"DQM full refresh · target {config.target} · {destination['database']}.{destination['schema']}",
          flush=True)
    print("Tracking history and reviewer annotations in these relations will be discarded:",
          flush=True)
    for item in workflow.settings["refresh"].values():
        print("  " + ".".join(str(item[key]) for key in ("database", "schema", "identifier")),
              flush=True)
    views = _views(manifest)
    if views:
        print("Review views will be rebuilt:", flush=True)
        for node in views:
            print(f"  {node.database}.{node.schema}.{node.alias}", flush=True)
    if workflow.request.get("yes"):
        return True
    import sys

    if not sys.stdin.isatty():
        raise ValueError("Noninteractive full refresh requires --yes. Review the reset scope "
                         "above, then add --yes to confirm discarding history and annotations.")
    try:
        return input("Reset these DQM relations? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _views(manifest):
    nodes = {key: node for key, node in manifest.nodes.items()
             if node.package_name == "dbt_dqm" and node.resource_type == "model"
             and node.config.materialized not in ("incremental", "table")}
    ordered = []

    def visit(key):
        if key in ordered:
            return
        for parent in nodes[key].depends_on.nodes:
            if parent in nodes:
                visit(parent)
        ordered.append(key)

    for key in nodes:
        visit(key)
    return [nodes[key] for key in reversed(ordered)]


def full_refresh(config, manifest, workflow):
    contract = workflow.settings["refresh"]
    if config.adapter_type == "bigquery":
        _bigquery_refresh(config, contract)
    else:
        workflow.view_grants = _postgres_refresh(config, contract, _views(manifest))


def _postgres_refresh(config, contract, views):
    from psycopg2 import sql

    types = {"string": "text", "timestamp": "timestamp", "int": "integer",
             "bigint": "bigint", "float": "double precision"}
    grants = []
    relation = lambda schema, name: sql.Identifier(schema, name)
    with warehouse._postgres_connection(config) as connection, connection.cursor() as cursor:
        # Same lock as reconciliation and annotation application. DROP without CASCADE rejects
        # external dependents; PostgreSQL rolls back the entire reset in that case.
        anchor = contract["dqm_install"]
        cursor.execute("select pg_advisory_xact_lock(hashtext(%s))",
                       ("dbt_dqm:" + anchor["schema"] + ("." + anchor["identifier"] if anchor["identifier"] != "dqm_install" else ""),))
        for schema in {item["schema"] for item in contract.values()}:
            cursor.execute(sql.SQL("create schema if not exists {}").format(sql.Identifier(schema)))
        for item in [*contract.values(), *[
            {"schema": node.schema, "identifier": node.alias} for node in views]]:
            cursor.execute("select grantee,privilege_type,is_grantable from information_schema.table_privileges "
                           "where table_schema=%s and table_name=%s",
                           (item["schema"], item["identifier"]))
            grants.extend((item["schema"], item["identifier"], *row) for row in cursor.fetchall())
        for node in views:
            cursor.execute(sql.SQL("drop view if exists {} restrict").format(relation(node.schema, node.alias)))
        for item in contract.values():
            table = relation(item["schema"], item["identifier"])
            cursor.execute(sql.SQL("drop table if exists {} restrict").format(table))
            columns = [sql.SQL("{} {}").format(sql.Identifier(name), sql.SQL(types[kind]))
                       for name, kind in item["columns"]]
            if item["keys"]:
                columns.append(sql.SQL("primary key ({})").format(
                    sql.SQL(",").join(map(sql.Identifier, item["keys"]))))
            cursor.execute(sql.SQL("create table {} ({})").format(table, sql.SQL(",").join(columns)))
        install = relation(anchor["schema"], anchor["identifier"])
        view_names = {(n.schema, n.alias) for n in views}
        pending = [grant for grant in grants if grant[:2] in view_names]
        cursor.execute(sql.SQL("insert into {} values (%s,current_timestamp,%s,%s)").format(install),
                       (CONTRACT, str(uuid.uuid4()), json.dumps(pending)))
        control = contract["dqm_reconciliation_control"]
        cursor.execute(sql.SQL("insert into {} (generation,setup_status) values (0,'ready')").format(
            relation(control["schema"], control["identifier"])))
        view_names = {(n.schema, n.alias) for n in views}
        for grant in grants:
            if grant[:2] not in view_names:
                _grant(cursor, grant)
    return [grant for grant in grants if grant[:2] in view_names]


def _grant(cursor, grant):
    from psycopg2 import sql

    schema, name, grantee, privilege, grantable = grant
    if privilege not in {"SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}:
        raise ValueError("Unsupported stored DQM relation privilege")
    # Privileges originate from PostgreSQL's catalog, never consumer input.
    principal = sql.SQL("PUBLIC") if grantee == "PUBLIC" else sql.Identifier(grantee)
    cursor.execute(sql.SQL("grant {} on {} to {}{}").format(
        sql.SQL(privilege), sql.Identifier(schema, name), principal,
        sql.SQL(" with grant option" if grantable == "YES" else "")))


def restore_view_grants(config):
    if config.adapter_type != "postgres":
        return
    with warehouse._postgres_connection(config) as connection, connection.cursor() as cursor:
        install = warehouse._table(config, "dqm_install")
        cursor.execute(f"select * from {install}")
        columns = [description.name for description in cursor.description]
        row = dict(zip(columns, cursor.fetchone(), strict=True))
        pending = json.loads(row.get("pending_view_grants") or "[]")
        if pending:
            for grant in pending:
                _grant(cursor, grant)
            cursor.execute(f"update {install} set pending_view_grants=null")


def _policy_access(policy):
    return sorted((binding["role"], tuple(sorted(binding.get("members", []))),
                   json.dumps(binding.get("condition"), sort_keys=True))
                  for binding in policy.to_api_repr().get("bindings", []))


def _bigquery_refresh(config, contract):
    from google.api_core.exceptions import NotFound
    from google.cloud import bigquery

    client = warehouse.client_for(config)
    types = {"string": "STRING", "timestamp": "TIMESTAMP", "int": "INT64",
             "bigint": "INT64", "float": "FLOAT64"}
    token = uuid.uuid4().hex
    replacements = []
    policies = {}
    try:
        for database, schema in {(item["database"], item["schema"]) for item in contract.values()}:
            dataset_id = f"{database}.{schema}"
            try:
                client.get_dataset(dataset_id)
            except NotFound:
                dataset = bigquery.Dataset(dataset_id)
                dataset.location = config.location
                client.create_dataset(dataset)
        # Prepare and inspect all destinations before overwriting any existing data.
        for name, item in contract.items():
            destination = f"{item['database']}.{item['schema']}.{item['identifier']}"
            stage = f"{item['database']}.{item['schema']}.{item['identifier']}__dqm_reset_{token}"
            try:
                existing = client.get_table(destination)
                policies[destination] = _policy_access(client.get_iam_policy(destination))
            except NotFound:
                existing = None
            previous = {field.name: field for field in existing.schema} if existing else {}
            desired_names = {column for column, _ in item["columns"]}
            if any(field.policy_tags and field.policy_tags.names for field in previous.values()
                   if field.name not in desired_names):
                raise ValueError(f"DQM reset cannot preserve a policy tag on a removed column at {destination}; review column access before resetting")
            fields = []
            for column, kind in item["columns"]:
                original = previous.get(column)
                fields.append(bigquery.SchemaField(
                    column, types[kind], mode="REQUIRED" if column in item["keys"] else
                    original.mode if original else "NULLABLE",
                    description=original.description if original else None,
                    policy_tags=original.policy_tags if original else None,
                    default_value_expression=original.default_value_expression if original else None))
            table = bigquery.Table(stage, schema=fields)
            if existing:
                if existing.table_type != "TABLE":
                    raise ValueError(f"Expected DQM table at {destination}; reset stopped")
                table.time_partitioning = existing.time_partitioning
                table.range_partitioning = existing.range_partitioning
                table.clustering_fields = existing.clustering_fields
                table.require_partition_filter = existing.require_partition_filter
                table.encryption_configuration = existing.encryption_configuration
                table.description = existing.description
                table.labels = existing.labels
                table.expires = existing.expires
            elif name in ("dqm_test_executions", "dqm_issue_observations", "dqm_issue_events"):
                field = {"dqm_test_executions": "captured_at", "dqm_issue_observations": "observed_at",
                         "dqm_issue_events": "event_at"}[name]
                table.time_partitioning = bigquery.TimePartitioning(field=field)
                table.clustering_fields = ["test_unique_id"]
            client.create_table(table)
            replacements.append((name, stage, destination))
        # A failed multi-table reset leaves the installation unreadable until an explicit retry.
        install = next(row for row in replacements if row[0] == "dqm_install")
        client.query(f"insert into `{install[1]}` values ('resetting',current_timestamp(),'{token}',null)").result()
        control = next(row for row in replacements if row[0] == "dqm_reconciliation_control")
        client.query(f"insert into `{control[1]}` (generation,setup_status) values (0,'ready')").result()
        ordered = [install, *[row for row in replacements if row != install]]
        for _, stage, destination in ordered:
            client.copy_table(stage, destination, job_config=bigquery.CopyJobConfig(
                write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE)).result()
            if destination in policies and _policy_access(client.get_iam_policy(destination)) != policies[destination]:
                raise RuntimeError(f"DQM reset permission verification failed for {destination}")
        client.query(f"update `{install[2]}` set marker='{CONTRACT}' where true").result()
    finally:
        for _, stage, _ in replacements:
            try:
                client.delete_table(stage, not_found_ok=True)
            except Exception as error:  # noqa: BLE001 - cleanup must not mask reset diagnostics.
                logging.getLogger(__name__).warning("DQM reset replacement cleanup failed for %s: %s", stage, type(error).__name__)

"""Optional reconciliation changes and workload, read from one statement snapshot."""

from __future__ import annotations

from .browse import Query
from .errors import SnapshotUnavailable


def owner_summary(config, reconciliation):
    from .warehouse import _read_with_state, _table

    parameter = "@run_id" if config.adapter_type == "bigquery" else "%(run_id)s"
    # Events carry the capture invocation, not the reconciliation invocation. Frozen inputs
    # identify exactly which evidence this run applied, including previously pending captures.
    sql = f"""with changes as (
        select event.occurrence_id,
            max(case when event.event_type='APPEARED' then 1 else 0 end) as opened,
            max(case when event.event_type='REAPPEARED' then 1 else 0 end) as recurring,
            max(case when event.event_type='DISAPPEARED' then 1 else 0 end) as archived
        from {_table(config, "dqm_issue_events")} event
        inner join {_table(config, "dqm_reconciliation_inputs")} input
            on input.test_unique_id=event.test_unique_id
            and input.invocation_id=event.invocation_id
        where input.run_id={parameter}
            and event.event_type in ('APPEARED','REAPPEARED','DISAPPEARED')
        group by event.occurrence_id
    ), counts as (
        select coalesce(nullif(trim(occ.dqm_owner),''),'Unassigned') as owner,
            sum(coalesce(changes.opened,0)) as opened_count,
            sum(coalesce(changes.recurring,0)) as recurring_count,
            sum(coalesce(changes.archived,0)) as archived_count,
            sum(case when occ.record_status='Active'
                and upper(coalesce(nullif(trim(occ.workflow_status),''),'NEW'))='NEW'
                then 1 else 0 end) as new_count,
            sum(case when occ.record_status='Active' then 1 else 0 end) as active_count
        from {_table(config, "dqm_issue_occurrences")} occ
        left join changes on changes.occurrence_id=occ.occurrence_id
        where occ.record_status='Active' or changes.occurrence_id is not null
        group by coalesce(nullif(trim(occ.dqm_owner),''),'Unassigned')
    )
    select counts.*, run.status as summary_run_status,
        run.generation_at_freeze as summary_generation_at_freeze
    from (select max(status) as status, max(generation_at_freeze) as generation_at_freeze
        from {_table(config, "dqm_reconciliation_runs")} where run_id={parameter}) run
    left join counts on true"""
    rows, generation = _read_with_state(config, Query(sql, {"run_id": reconciliation}))
    if not rows or any(
        row.get("summary_run_status") != "completed"
        or row.get("summary_generation_at_freeze") is None
        or row["summary_generation_at_freeze"] + 1 != generation[1]
        for row in rows
    ):
        raise SnapshotUnavailable(
            "The reconciliation is unavailable or its generation has changed. Summary unavailable."
        )
    fields = ("opened", "recurring", "archived", "new", "active")
    owners = [
        {"owner": row["owner"], **{field: int(row[field + "_count"]) for field in fields}}
        for row in rows
        if row.get("owner") is not None
    ]
    if not any(row["owner"] == "Unassigned" for row in owners):
        owners.append({"owner": "Unassigned", **dict.fromkeys(fields, 0)})
    owners.sort(key=lambda row: (-row["new"], row["owner"].casefold(), row["owner"]))
    return {
        **{field: sum(row[field] for row in owners) for field in fields},
        "owners": owners,
        "generation": generation,
        "reconciliation_id": reconciliation,
    }


def print_summary(summary):
    rows = [
        {**summary, "owner": "Total"},
        *summary["owners"],
    ]
    width = max(5, *(len(row["owner"]) for row in rows))
    columns = (("opened", "OPENED (NEW)"), ("recurring", "RECURRING"),
               ("archived", "ARCHIVED"), ("new", "NEW (status)"), ("active", "ACTIVE"))
    print("DQM owner summary:", flush=True)
    print(f"{'Owner':<{width}}  " + "  ".join(label for _, label in columns), flush=True)
    for row in rows:
        print(f"{row['owner']:<{width}}  " + "  ".join(
            f"{row[field]:>{len(label)}}" for field, label in columns), flush=True)
    print("OPENED (NEW), RECURRING and ARCHIVED are changes applied by this reconciliation. "
          "NEW (status) is the active review workload with status NEW; ACTIVE includes it.",
          flush=True)

"""Optional current workload report, read from one warehouse statement snapshot."""

from __future__ import annotations

from .browse import Query
from .errors import SnapshotUnavailable


def owner_summary(config, reconciliation):
    from .warehouse import _read_with_state, _table

    sql = f"""select coalesce(nullif(trim(dqm_owner),''),'Unassigned') as owner,
        sum(case when upper(coalesce(nullif(trim(workflow_status),''),'NEW'))='NEW'
                 then 1 else 0 end) as new_count, count(*) as active_count
        from {_table(config, "dqm_issue_occurrences")}
        where record_status='Active'
        group by coalesce(nullif(trim(dqm_owner),''),'Unassigned')"""
    # The run check is in the same statement snapshot as the counts and installation marker.
    parameter = "@run_id" if config.adapter_type == "bigquery" else "%(run_id)s"
    sql = f"""select counts.*, run.status as summary_run_status from
        (select max(status) as status from {_table(config, "dqm_reconciliation_runs")}
         where run_id={parameter}) run left join ({sql}) counts on true"""
    rows, generation = _read_with_state(config, Query(sql, {"run_id": reconciliation}))
    if any(row.get("summary_run_status") != "completed" for row in rows):
        raise SnapshotUnavailable("The reconciliation is no longer available. Summary unavailable.")
    owners = [
        {"owner": row["owner"], "new": int(row["new_count"]), "active": int(row["active_count"])}
        for row in rows
        if row.get("owner") is not None
    ]
    if not any(row["owner"] == "Unassigned" for row in owners):
        owners.append({"owner": "Unassigned", "new": 0, "active": 0})
    owners.sort(key=lambda row: (-row["new"], row["owner"].casefold(), row["owner"]))
    return {
        "new": sum(row["new"] for row in owners),
        "active": sum(row["active"] for row in owners),
        "owners": owners,
        "generation": generation,
    }


def print_summary(summary):
    rows = [
        {"owner": "Total", "new": summary["new"], "active": summary["active"]},
        *summary["owners"],
    ]
    width = max(5, *(len(row["owner"]) for row in rows))
    print("DQM owner summary (all active issues; NEW is included in ACTIVE):", flush=True)
    print(f"{'Owner':<{width}}  {'NEW':>6}  {'ACTIVE':>6}", flush=True)
    for row in rows:
        print(f"{row['owner']:<{width}}  {row['new']:>6}  {row['active']:>6}", flush=True)

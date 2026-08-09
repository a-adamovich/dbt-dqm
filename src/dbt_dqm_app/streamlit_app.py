from __future__ import annotations

import os
import time
from concurrent.futures import Future, ProcessPoolExecutor

import pandas as pd
import streamlit as st

from dbt_dqm_app.config import load_config
from dbt_dqm_app.display import (
    WORKFLOW_STATUSES,
    calendar_date,
    column_value_pairs,
    format_timestamp,
    owner_editor_options,
    owner_label,
    owner_value_from_selection,
    paired_display,
    record_fields_html,
    workflow_status_options,
)
from dbt_dqm_app.store import EDITABLE_FIELDS, Workspace
from dbt_dqm_app.warehouse import apply_worker, sync_worker


def run_app() -> None:
    st.set_page_config(page_title="dbt-dqm", layout="wide")
    st.title("dbt-dqm issue review")

    config = load_config(
        os.environ["DBT_DQM_PROJECT_DIR"],
        os.environ["DBT_DQM_PROFILES_DIR"],
        os.environ["DBT_DQM_TARGET"],
    )
    workspace = Workspace(config.workspace_path)
    st.caption(f"{config.project_id}.{config.dataset} · target {config.target}")
    st.markdown(
        """
        <style>
          div[data-testid="stLayoutWrapper"]:has(> .st-key-dqm-action-bar) {
            position: sticky;
            top: 3.25rem;
            z-index: 90;
            background-color: rgb(14, 17, 23);
            border-bottom: 1px solid rgba(128, 128, 128, 0.28);
            box-shadow: 0 0.35rem 0.45rem rgb(14, 17, 23);
          }
          .st-key-dqm-action-bar { padding: 0.75rem 0 0.55rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    for key, default in (
        ("executor", None),
        ("job", None),
        ("job_kind", None),
        ("job_patches", []),
        ("job_notice", None),
        ("startup_sync_started", False),
    ):
        if key not in st.session_state:
            st.session_state[key] = default
    if st.session_state.executor is None:
        st.session_state.executor = ProcessPoolExecutor(max_workers=1)

    # Start each review session from current warehouse data. SQLite patches survive the refresh
    # and are overlaid after the new snapshot is installed.
    if not st.session_state.startup_sync_started and st.session_state.job is None:
        st.session_state.startup_sync_started = True
        st.session_state.job_kind = "sync"
        st.session_state.job = st.session_state.executor.submit(sync_worker, config)

    job: Future | None = st.session_state.job
    if job is not None and job.done():
        try:
            if st.session_state.job_kind == "apply":
                batch_id, rows = job.result()
                workspace.replace_snapshot(rows)
                workspace.clear_applied(st.session_state.job_patches)
                st.session_state.job_notice = (
                    "success",
                    f"Applied batch {batch_id} and synchronized local data.",
                )
            else:
                workspace.replace_snapshot(job.result())
                st.session_state.job_notice = ("success", "Local data synchronized.")
        except Exception as error:  # noqa: BLE001 - patches must survive worker failures.
            st.session_state.job_notice = ("error", f"Background job failed: {error}")
        finally:
            st.session_state.job = None
            st.session_state.job_kind = None
            st.session_state.job_patches = []
            st.rerun()

    notice = st.session_state.job_notice
    if notice is not None:
        level, message = notice
        getattr(st, level)(message)
        st.session_state.job_notice = None

    busy = st.session_state.job is not None
    pending = workspace.pending()
    with st.container(key="dqm-action-bar"):
        col_sync, col_apply, col_state = st.columns([1, 1.35, 3])
        with col_sync:
            if st.button("Sync", disabled=busy):
                st.session_state.job_kind = "sync"
                st.session_state.job = st.session_state.executor.submit(sync_worker, config)
                st.rerun()
        with col_apply:
            if st.button("Apply and sync", disabled=busy or not pending, type="primary"):
                frozen = list(pending)
                st.session_state.job_patches = frozen
                st.session_state.job_kind = "apply"
                st.session_state.job = st.session_state.executor.submit(apply_worker, config, frozen)
                st.rerun()
        with col_state:
            if busy:
                st.info(f"Background {st.session_state.job_kind} is running…")
            else:
                st.write(
                    f"Pending changes: **{len(pending)}** · "
                    f"Last sync: {format_timestamp(workspace.last_sync(), 'Never')}"
                )

    # Streamlit does not rerun merely because a Future completes. Poll while a serialized
    # worker job is active so completion or failure reaches the UI without another click.
    if busy:
        time.sleep(0.5)
        st.rerun()

    rows = workspace.rows()
    if not rows:
        st.info("No local data. Select Sync after building the dbt-dqm package models.")
        st.stop()

    frame = pd.DataFrame(rows)
    required_columns = {
        "occurrence_id",
        "record_status",
        "test_name",
        "test_status",
        "first_seen_at",
        "record_values_json",
    }
    missing_columns = sorted(required_columns - set(frame.columns))
    if missing_columns:
        st.error(
            "Synchronized issue data is missing required columns: "
            f"{', '.join(missing_columns)}. Rebuild the dbt-dqm package models and sync again."
        )
        st.stop()

    if "_dirty" not in frame:
        frame["_dirty"] = False
    else:
        frame["_dirty"] = frame["_dirty"].map(
            lambda value: bool(value) if pd.notna(value) else False
        )
    frame["record_details"] = frame.apply(
        lambda row: paired_display(row.get("record_values_json")),
        axis=1,
    )
    frame["test_status"] = frame["test_status"].fillna("NEW").astype(str).str.upper()
    frame["_created_date"] = frame["first_seen_at"].map(calendar_date)
    if "poc_responsible" not in frame:
        frame["poc_responsible"] = None
    frame["owner"] = frame["poc_responsible"].map(owner_label)
    editor_owner_options = owner_editor_options(frame["poc_responsible"])

    active_count = int((frame["record_status"] == "Active").sum())
    new_count = int(
        ((frame["record_status"] == "Active") & (frame["test_status"] == "NEW")).sum()
    )
    blocked_count = int(
        ((frame["record_status"] == "Active") & (frame["test_status"] == "BLOCKED")).sum()
    )
    metric_active, metric_new, metric_blocked, metric_local = st.columns(4)
    metric_active.metric("Active issues", active_count)
    metric_new.metric("New", new_count)
    metric_blocked.metric("Blocked", blocked_count)
    metric_local.metric("Pending edits", len(pending))

    lifecycle_options = sorted(frame["record_status"].dropna().unique())
    lifecycle_default = ["Active"] if "Active" in lifecycle_options else lifecycle_options
    status_options = [
        status
        for status in WORKFLOW_STATUSES
        if status in set(frame["test_status"].dropna().unique())
    ]
    status_options.extend(
        sorted(set(frame["test_status"].dropna().unique()) - set(status_options))
    )
    owner_options = sorted(frame["owner"].unique())
    lifecycle_col, status_col, owner_col, created_col, search_col = st.columns(
        [0.9, 1.2, 1.45, 1.65, 2]
    )
    with lifecycle_col:
        lifecycle = st.multiselect(
            "Lifecycle", lifecycle_options, default=lifecycle_default
        )
    with status_col:
        statuses = st.multiselect("Workflow status", status_options, default=status_options)
    with owner_col:
        owners = st.multiselect(
            "Owner",
            owner_options,
            default=[],
            placeholder="All owners",
        )
    created_dates = frame["_created_date"].dropna()
    with created_col:
        if created_dates.empty:
            created_range = ()
            st.caption("Created date unavailable")
        else:
            created_range = st.date_input(
                "Created date",
                value=(created_dates.min(), created_dates.max()),
                min_value=created_dates.min(),
                max_value=created_dates.max(),
                format="YYYY-MM-DD",
                help="Show issues created within this inclusive local calendar-date range.",
            )
    with search_col:
        search = st.text_input("Search")

    visible = frame[frame["record_status"].isin(lifecycle)] if lifecycle else frame
    if statuses:
        visible = visible[visible["test_status"].isin(statuses)]
    if owners:
        visible = visible[visible["owner"].isin(owners)]
    if len(created_range) == 2:
        created_start, created_end = created_range
        visible = visible[
            visible["_created_date"].between(created_start, created_end, inclusive="both")
        ]
    if search:
        searchable = visible.astype(str).apply(
            lambda column: column.str.contains(search, case=False, na=False)
        )
        visible = visible[searchable.any(axis=1)]
    priority = {
        "NEW": 0,
        "BLOCKED": 1,
        "IN_PROGRESS": 2,
        "TRIAGED": 3,
        "RESOLVED": 4,
        "ACCEPTED_RISK": 5,
        "FALSE_POSITIVE": 6,
    }
    visible = visible.copy()
    visible["_status_priority"] = visible["test_status"].map(priority).fillna(99)
    visible["_created_sort"] = pd.to_datetime(visible.get("first_seen_at"), errors="coerce")
    visible = visible.sort_values(
        ["_dirty", "_status_priority", "_created_sort"],
        ascending=[False, True, True],
        na_position="last",
    )

    st.caption(f"Showing {len(visible)} issue record(s)")
    staged_change = False
    sync_token = workspace.last_sync() or "never"
    for _, row in visible.iterrows():
        occurrence_id = str(row["occurrence_id"])
        test_name = str(row.get("test_name") or "Unnamed test")
        widget_prefix = f"{occurrence_id}-{sync_token}"
        with st.container(border=True):
            title_col, owner_col, workflow_col = st.columns([4.5, 2, 1.5])
            with title_col:
                st.subheader(test_name)
                state_text = str(row.get("record_status") or "Unknown")
                if bool(row.get("_dirty")):
                    state_text += " · Unsaved local changes"
                st.caption(state_text)
            with owner_col:
                current_owner = owner_value_from_selection(row.get("poc_responsible"))
                owner_selection = st.selectbox(
                    "Owner",
                    editor_owner_options,
                    index=editor_owner_options.index(
                        current_owner if current_owner is not None else "— Unassigned —"
                    ),
                    key=f"{widget_prefix}-poc_responsible",
                    accept_new_options=True,
                    placeholder="Select or type an owner",
                    help="Choose an existing owner or type a new one.",
                )
                poc_responsible = owner_value_from_selection(owner_selection)
            with workflow_col:
                current_status = str(row.get("test_status") or "NEW").upper()
                status_choices = workflow_status_options(current_status)
                test_status = st.selectbox(
                    "Status",
                    status_choices,
                    index=status_choices.index(current_status),
                    key=f"{widget_prefix}-test_status",
                )
            st.caption(
                f"Created: {format_timestamp(row.get('first_seen_at'))} · "
                f"User updated: {format_timestamp(row.get('annotation_updated_at'), 'Never')}"
            )

            record_pairs = column_value_pairs(row.get("record_values_json"))
            st.markdown("**Record fields**")
            st.html(record_fields_html(record_pairs))

            st.caption("Call to action")
            st.write(row.get("call_to_action") or "Not specified")

            with st.expander("Edit annotations", key=f"edit-{occurrence_id}"):
                ticket_url = st.text_input(
                    "Ticket URL",
                    value="" if pd.isna(row.get("ticket_url")) else str(row["ticket_url"]),
                    key=f"{widget_prefix}-ticket_url",
                )
                call_to_action = st.text_area(
                    "Call to action",
                    value=""
                    if pd.isna(row.get("call_to_action"))
                    else str(row["call_to_action"]),
                    key=f"{widget_prefix}-call_to_action",
                    height=90,
                )
                notes = st.text_area(
                    "Notes",
                    value="" if pd.isna(row.get("notes")) else str(row["notes"]),
                    key=f"{widget_prefix}-notes",
                    height=120,
                )
            with st.expander("Technical details", key=f"technical-{occurrence_id}"):
                st.text(f"Issue ID: {occurrence_id}")
                st.text(f"Identity: {row.get('unique_id') or '—'}")
                st.text(f"Tags: {row.get('test_tags') or '[]'}")
                st.text(f"First seen: {format_timestamp(row.get('first_seen_at'))}")
                st.text(f"Last seen: {format_timestamp(row.get('last_seen_at'))}")

            edited_values = {
                "test_status": test_status,
                "poc_responsible": poc_responsible,
                "ticket_url": ticket_url,
                "call_to_action": call_to_action,
                "notes": notes,
            }
            for field in EDITABLE_FIELDS:
                before = row.get(field)
                before = None if pd.isna(before) else str(before)
                after = edited_values[field] or None
                if before != after:
                    workspace.set_change(occurrence_id, field, after)
                    staged_change = True

    if staged_change:
        st.rerun()

    if workspace.pending():
        st.warning("There are unapplied local changes. Apply them before closing the application.")


if __name__ == "__main__":
    run_app()

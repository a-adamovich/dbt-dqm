from __future__ import annotations

import math
import os
import time
import uuid
from concurrent.futures import Future, ProcessPoolExecutor
from datetime import UTC, datetime

import pandas as pd
import streamlit as st

from dbt_dqm_app.config import load_config
from dbt_dqm_app.display import (
    REVIEW_VERDICTS,
    WORKFLOW_STATUSES,
    calendar_date,
    column_value_pairs,
    format_timestamp,
    owner_editor_options,
    owner_label,
    owner_value_from_selection,
    paired_display,
    priority_rank,
    record_fields_html,
    tag_list,
    workflow_status_help,
    workflow_status_options,
)
from dbt_dqm_app.store import EDITABLE_FIELDS, Workspace
from dbt_dqm_app.warehouse import (
    MISSED_ROOT_CAUSES,
    apply_worker,
    insert_missed_issue,
    manifest_nodes,
    sync_worker,
)

PAGE_SIZE_OPTIONS = [25, 50, 100, 200]


@st.cache_resource
def _get_executor() -> ProcessPoolExecutor:
    """One process pool shared by every session for the life of the app process, instead of one
    per session. A per-session pool (the previous design) never got shut down when a session
    ended, leaking one background process per browser tab opened over the app's lifetime."""
    return ProcessPoolExecutor(max_workers=1)


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

    executor = _get_executor()
    for key, default in (
        ("job", None),
        ("job_kind", None),
        ("job_patches", []),
        ("job_notice", None),
        ("startup_sync_started", False),
        ("allow_drift_overwrite", False),
    ):
        if key not in st.session_state:
            st.session_state[key] = default

    # Start each review session from current warehouse data. SQLite patches survive the refresh
    # and are overlaid after the new snapshot is installed.
    if not st.session_state.startup_sync_started and st.session_state.job is None:
        st.session_state.startup_sync_started = True
        st.session_state.job_kind = "sync"
        st.session_state.job = executor.submit(sync_worker, config)

    job: Future | None = st.session_state.job
    if job is not None and job.done():
        try:
            if st.session_state.job_kind == "apply":
                batch_id, snapshot = job.result()
                workspace.clear_applied(st.session_state.job_patches)
                workspace.replace_snapshot(snapshot["issues"])
                workspace.replace_health(snapshot["health"])
                st.session_state.job_notice = (
                    "success",
                    f"Applied batch {batch_id} and synchronized local data.",
                )
            else:
                snapshot = job.result()
                workspace.replace_snapshot(snapshot["issues"])
                workspace.replace_health(snapshot["health"])
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
    drifted = workspace.drifted_patches() if not busy else []
    with st.container(key="dqm-action-bar"):
        col_sync, col_apply, col_state = st.columns([1, 1.35, 3])
        with col_sync:
            if st.button("Sync", disabled=busy):
                st.session_state.job_kind = "sync"
                st.session_state.job = executor.submit(sync_worker, config)
                st.rerun()
        with col_apply:
            if st.button(
                "Apply and sync",
                disabled=busy
                or not pending
                or (bool(drifted) and not st.session_state.allow_drift_overwrite),
                type="primary",
            ):
                frozen = list(pending)
                st.session_state.job_patches = frozen
                st.session_state.job_kind = "apply"
                st.session_state.job = executor.submit(apply_worker, config, frozen)
                st.rerun()
        with col_state:
            if busy:
                st.info(f"Background {st.session_state.job_kind} is running…")
            else:
                st.write(
                    f"Pending changes: **{len(pending)}** · "
                    f"Last sync: {format_timestamp(workspace.last_sync(), 'Never')}"
                )

    if not busy:
        if drifted:
            preview = ", ".join(
                f"{patch.occurrence_id[:8]}…/{patch.field_name}" for patch in drifted[:5]
            )
            more = f", and {len(drifted) - 5} more" if len(drifted) > 5 else ""
            st.warning(
                f"{len(drifted)} pending edit(s) were staged against a warehouse value that has "
                f"since changed: {preview}{more}. Choose whether to discard those local edits or "
                "explicitly overwrite the newer warehouse values."
            )
            discard_col, overwrite_col, _ = st.columns([1.2, 1.4, 3])
            with discard_col:
                if st.button("Discard conflicts"):
                    workspace.discard_patches(drifted)
                    st.session_state.allow_drift_overwrite = False
                    st.rerun()
            with overwrite_col:
                if st.button("Allow overwrite", type="secondary"):
                    workspace.rebase_patches(drifted)
                    st.session_state.allow_drift_overwrite = True
                    st.rerun()
        elif st.session_state.allow_drift_overwrite:
            st.session_state.allow_drift_overwrite = False

    # Streamlit does not rerun merely because a Future completes. Poll while a serialized
    # worker job is active so completion or failure reaches the UI without another click.
    if busy:
        time.sleep(0.5)
        st.rerun()

    issues_tab, health_tab, missed_tab = st.tabs(["Issues", "Health", "Report missed issue"])
    with issues_tab:
        _render_issues(workspace, pending)
    with health_tab:
        _render_health(workspace)
    with missed_tab:
        _render_missed_form(config)


def _render_issues(workspace, pending) -> None:
    rows = workspace.rows()
    if not rows:
        st.info("No local data. Select Sync after building the dbt-dqm package models.")
        return

    frame = pd.DataFrame(rows)
    required_columns = {
        "occurrence_id",
        "record_status",
        "test_name",
        "workflow_status",
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
    frame["workflow_status"] = frame["workflow_status"].fillna("NEW").astype(str).str.upper()
    frame["_created_date"] = frame["first_seen_at"].map(calendar_date)
    if "poc_responsible" not in frame:
        frame["poc_responsible"] = None
    frame["owner"] = frame["poc_responsible"].map(owner_label)
    editor_owner_options = owner_editor_options(frame["poc_responsible"])

    active_count = int((frame["record_status"] == "Active").sum())
    new_count = int(
        ((frame["record_status"] == "Active") & (frame["workflow_status"] == "NEW")).sum()
    )
    blocked_count = int(
        ((frame["record_status"] == "Active") & (frame["workflow_status"] == "BLOCKED")).sum()
    )
    metric_active, metric_new, metric_blocked, metric_local, metric_recurring = st.columns(5)
    metric_recurring.metric(
        "Recurring",
        sum(
            row.get("record_status") == "Active" and bool(row.get("is_recurrence")) for row in rows
        ),
    )
    metric_active.metric("Active issues", active_count)
    metric_new.metric("New", new_count)
    metric_blocked.metric("Blocked", blocked_count)
    metric_local.metric("Pending edits", len(pending))

    lifecycle_options = sorted(frame["record_status"].dropna().unique())
    lifecycle_default = ["Active"] if "Active" in lifecycle_options else lifecycle_options
    status_options = [
        status
        for status in WORKFLOW_STATUSES
        if status in set(frame["workflow_status"].dropna().unique())
    ]
    status_options.extend(
        sorted(set(frame["workflow_status"].dropna().unique()) - set(status_options))
    )
    owner_options = sorted(frame["owner"].unique())
    lifecycle_col, status_col, owner_col, created_col, search_col = st.columns(
        [0.9, 1.2, 1.45, 1.65, 2]
    )
    with lifecycle_col:
        lifecycle = st.multiselect("Lifecycle", lifecycle_options, default=lifecycle_default)
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
        # A plain text_input reruns the whole script (re-filtering and re-stringifying every
        # column of every row) on every keystroke. Wrapping it in a form defers that expensive
        # search until Enter/the submit button, while every other filter above stays live.
        with st.form("dqm-search-form", border=False):
            search_field, search_button = st.columns([3, 1])
            with search_field:
                search_draft = st.text_input(
                    "Search", value=st.session_state.get("applied_search", "")
                )
            with search_button:
                st.markdown("<div style='height: 1.6rem'></div>", unsafe_allow_html=True)
                search_submitted = st.form_submit_button("Apply")
        if search_submitted:
            st.session_state.applied_search = search_draft
        search = st.session_state.get("applied_search", "")

    priority_col, tags_col, recurring_col = st.columns(3)
    with priority_col:
        priorities = st.multiselect("Priority", ["critical", "high", "medium", "low", "unset"])
    with tags_col:
        all_tags = sorted({tag for row in rows for tag in tag_list(row.get("test_tags"))})
        tags = st.multiselect("Tags", all_tags)
    with recurring_col:
        recurring_only = st.checkbox("Recurrences only")
    pending_only = st.checkbox("Pending edits only", help="Includes edits hidden by other filters.")
    visible = frame[frame["record_status"].isin(lifecycle)] if lifecycle else frame
    if priorities:
        visible = visible[
            visible.get("test_priority", pd.Series(index=visible.index, dtype=object))
            .fillna("unset")
            .isin(priorities)
        ]
    if tags:
        visible = visible[
            visible["test_tags"].map(lambda value: bool(set(tag_list(value)) & set(tags)))
        ]
    if recurring_only:
        visible = visible[
            visible.get("is_recurrence", pd.Series(False, index=visible.index)).fillna(False)
        ]
    if statuses:
        visible = visible[visible["workflow_status"].isin(statuses)]
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
    if pending_only:
        visible = frame[frame["_dirty"]]
    visible = visible.copy()
    visible["_priority"] = visible.get(
        "test_priority", pd.Series(index=visible.index, dtype=object)
    ).map(priority_rank)
    visible["_status_priority"] = visible["workflow_status"].map(priority).fillna(99)
    visible["_created_sort"] = pd.to_datetime(visible.get("first_seen_at"), errors="coerce")
    visible = visible.sort_values(
        ["_dirty", "_priority", "_status_priority", "_created_sort", "occurrence_id"],
        ascending=[False, True, True, True, True],
        na_position="last",
    )

    # Rendering every visible issue as a full record card (per the app's design — no dense grid
    # rows) doesn't scale past a few hundred issues if all of them render on one script run: each
    # card registers several widgets, so page size and DOM size both grow with the filtered count.
    # Paginate the already-filtered, already-sorted frame instead of rendering it in full.
    total_visible = len(visible)
    page_size_col, page_number_col, page_caption_col = st.columns([1, 1, 3])
    with page_size_col:
        page_size = st.selectbox("Per page", PAGE_SIZE_OPTIONS, index=0, key="dqm-page-size")
    total_pages = max(1, math.ceil(total_visible / page_size))
    page_key = "dqm-page-number"
    if page_key in st.session_state and st.session_state[page_key] > total_pages:
        st.session_state[page_key] = total_pages
    with page_number_col:
        page_number = st.number_input(
            "Page", min_value=1, max_value=total_pages, step=1, value=1, key=page_key
        )
    start = (page_number - 1) * page_size
    page_rows = visible.iloc[start : start + page_size]
    with page_caption_col:
        st.markdown("<div style='height: 1.6rem'></div>", unsafe_allow_html=True)
        st.caption(
            f"Showing {len(page_rows)} of {total_visible} issue record(s) "
            f"(page {page_number} of {total_pages})"
        )
    staged_change = False
    sync_token = workspace.last_sync() or "never"
    for _, row in page_rows.iterrows():
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
                current_status = str(row.get("workflow_status") or "NEW").upper()
                status_choices = workflow_status_options(current_status)
                workflow_status = st.selectbox(
                    "Status",
                    status_choices,
                    index=status_choices.index(current_status),
                    key=f"{widget_prefix}-workflow_status",
                    help=workflow_status_help(),
                )
            if row.get("_outside_cache") is True:
                st.warning(
                    "This pending edit is outside the synchronized issue window. Synchronize or discard it before applying."
                )
            st.caption(
                f"Created: {format_timestamp(row.get('first_seen_at'))} · "
                f"User updated: {format_timestamp(row.get('annotation_updated_at'), 'Never')}"
            )

            badges = tag_list(row.get("test_tags"))
            for field, label in (
                ("test_priority", "Priority"),
                ("test_criticality", "Criticality"),
            ):
                if pd.notna(row.get(field)) and row.get(field):
                    badges.insert(0, f"{label}: {row[field]}")
            if badges:
                st.badge(" · ".join(badges))
            if row.get("has_previous_occurrence"):
                banner = (
                    f"Previous occurrence closed {format_timestamp(row.get('previous_archived_at'))} "
                    f"({row.get('previous_close_reason')}); workflow at close: "
                    f"{row.get('previous_workflow_status_at_close') or 'unknown'}."
                )
                if row.get("is_potential_regression"):
                    st.warning("Potential regression. " + banner)
                else:
                    st.info(
                        ("Recurrence. " if row.get("is_recurrence") else "Previous history. ")
                        + banner
                    )
                with st.expander("Previous investigation", key=f"previous-{occurrence_id}"):
                    st.write(row.get("previous_ticket_url") or "No previous ticket")
                    st.write(row.get("previous_notes") or "No previous notes")
            current_verdict = row.get("review_verdict") or "UNREVIEWED"
            review_verdict = st.selectbox(
                "Review verdict",
                REVIEW_VERDICTS,
                index=REVIEW_VERDICTS.index(current_verdict),
                key=f"{widget_prefix}-review_verdict",
                help="Classify whether this occurrence is a real issue, independently of its workflow status.",
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
                    value="" if pd.isna(row.get("call_to_action")) else str(row["call_to_action"]),
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
                "workflow_status": workflow_status,
                "review_verdict": review_verdict,
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


def _render_health(workspace) -> None:
    health = workspace.health_rows()
    if not health:
        st.info("Synchronize after building the health views.")
        return
    st.caption(f"Last health sync: {format_timestamp(health.get('synced_at'))}")
    st.caption(
        "Review: occurrences created in the reporting window. Resolution: Passed closures in the window. Backlog: current Active issues."
    )
    for kind, label in (("tests", "Test health"), ("areas", "Area health")):
        st.subheader(label)
        frame = pd.DataFrame(health.get(kind, []))
        if frame.empty:
            st.info("No tracked data in this population.")
        else:
            if "attention_flags" in frame:
                frame["_flags"] = frame["attention_flags"].map(
                    lambda value: len([v for v in str(value).split(",") if v])
                )
                frame = frame.sort_values("_flags", ascending=False).drop(columns="_flags")
            st.dataframe(frame, hide_index=True)
    st.caption(
        "Area totals overlap: a test belongs to each direct model/source dependency. Known misses are reported observations, not a false-negative rate."
    )


def _render_missed_form(config) -> None:
    try:
        nodes = manifest_nodes(config)
    except (OSError, ValueError):
        nodes = {}
    areas = {
        id: node
        for id, node in nodes.items()
        if node.get("resource_type") in ("model", "source")
        and node.get("package_name") != "dbt_dqm"
    }
    tests = {
        id: node
        for id, node in nodes.items()
        if node.get("resource_type") == "test" and node.get("package_name") != "dbt_dqm"
    }
    if "missed_submission_id" not in st.session_state:
        st.session_state.missed_submission_id = str(uuid.uuid4())
    with st.form("missed-issue"):
        source = st.selectbox(
            "Affected model or source",
            [None, *sorted(areas)],
            format_func=lambda id: "Unmapped area" if id is None else id,
        )
        area_label = st.text_input("Area name (required for an unmapped area)")
        test = st.selectbox(
            "Test that should have detected it",
            [None, *sorted(tests)],
            format_func=lambda id: id or "Unknown / no test",
        )
        discovered = st.date_input("Discovered on")
        description = st.text_area("Description")
        evidence = st.text_input("Evidence URL")
        priority = st.selectbox(
            "Priority",
            [None, "critical", "high", "medium", "low"],
            format_func=lambda v: v or "Unset",
        )
        detection = st.text_input("How it was detected")
        root_cause = st.selectbox("Root cause", MISSED_ROOT_CAUSES)
        submitted = st.form_submit_button("Report missed issue")
    if submitted:
        issue = {
            "missed_issue_id": st.session_state.missed_submission_id,
            "discovered_at": datetime.combine(discovered, datetime.min.time(), tzinfo=UTC),
            "source_unique_id": source,
            "area_label": area_label or None,
            "test_unique_id": test,
            "description": description,
            "evidence_url": evidence or None,
            "priority": priority,
            "detection_method": detection or None,
            "root_cause": root_cause,
        }
        try:
            insert_missed_issue(config, issue)
        except Exception as error:  # noqa: BLE001 - retain submission ID for safe retry.
            st.error(f"Report could not be confirmed; retry uses the same ID. {error}")
        else:
            st.success("Missed issue recorded. Synchronize to refresh health reporting.")
            st.session_state.missed_submission_id = str(uuid.uuid4())


if __name__ == "__main__":
    run_app()

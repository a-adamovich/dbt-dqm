from __future__ import annotations

import logging
import math
import os
import tempfile
import time
import uuid
from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime

import pandas as pd
import streamlit as st

from dbt_dqm_app.browse import (
    CHUNK_ISSUES,
    PAGE_SIZES,
    Filters,
    InvalidSearch,
    normalized_search,
)
from dbt_dqm_app.config import load_config
from dbt_dqm_app.display import (
    REVIEW_VERDICTS,
    WORKFLOW_STATUSES,
    calendar_date,
    column_value_pairs,
    format_timestamp,
    maintenance_table,
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
from dbt_dqm_app.errors import SearchTooCostly, classify
from dbt_dqm_app.exports import matches_filters, prepare_export
from dbt_dqm_app.frames import configure_pandas
from dbt_dqm_app.limits import MAX_CACHE_ISSUES, MAX_CACHE_MIB, CacheLimitExceeded, check_cache_size
from dbt_dqm_app.selection import Selection
from dbt_dqm_app.store import EDITABLE_FIELDS, Workspace
from dbt_dqm_app.warehouse import (
    MISSED_ROOT_CAUSES,
    apply_worker,
    browse_options,
    browse_page,
    browse_population,
    browse_refresh_worker,
    browse_summary,
    insert_missed_issue,
    manifest_nodes,
    run_worker,
    sync_worker,
)

configure_pandas()

PAGE_SIZE_OPTIONS = [25, 50, 100, 200]

logger = logging.getLogger(__name__)


@st.cache_resource
def _get_executor() -> ProcessPoolExecutor:
    """One process pool shared by every session for the life of the app process, instead of one
    per session. A per-session pool (the previous design) never got shut down when a session
    ended, leaking one background process per browser tab opened over the app's lifetime."""
    return ProcessPoolExecutor(max_workers=1)


def run_app() -> None:
    st.set_page_config(page_title="dbt-dqm", layout="wide")
    st.title("dbt-dqm issue review")

    try:
        config = load_config(
            os.environ.get("DBT_DQM_PROJECT_DIR"),
            os.environ.get("DBT_DQM_PROFILES_DIR"),
            os.environ.get("DBT_DQM_TARGET"),
        )
    except Exception as error:  # noqa: BLE001 - configuration errors belong in the UI.
        failure = classify(error)
        st.error(failure.message)
        if failure.details:
            with st.expander("Technical details"):
                st.code(failure.details, language=None)
        st.stop()
        return
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
        ("browsing", config.browse_mode == "warehouse"),
    ):
        if key not in st.session_state:
            st.session_state[key] = default

    # Start each review session from current warehouse data. SQLite patches survive the refresh
    # and are overlaid after the new snapshot is installed.
    if not st.session_state.startup_sync_started and st.session_state.job is None:
        st.session_state.startup_sync_started = True
        _submit_sync(executor, config)

    if st.session_state.pop("browse_switch", False) and st.session_state.job is None:
        _submit_sync(executor, config)  # Health for the warehouse-mode session

    job: Future | None = st.session_state.job
    if job is not None and job.done():
        try:
            if st.session_state.job_kind == "export":
                st.session_state.prepared_export = job.result()
                st.session_state.job_notice = ("success", "Export prepared from saved values.", "")
            elif st.session_state.job_kind == "apply":
                batch_id, snapshot, refresh_error = job.result()
                _clear_selection()
                workspace.clear_applied(st.session_state.job_patches)
                _reset_browse_cache()
                if refresh_error is None:
                    try:
                        _install_snapshot(workspace, config, snapshot)
                    except Exception as error:  # noqa: BLE001 - Apply already committed.
                        refresh_error = classify(error)
                if refresh_error is None:
                    st.session_state.job_notice = (
                        "success",
                        f"Applied batch {batch_id} and synchronized local data.",
                        "",
                    )
                else:
                    st.session_state.job_notice = (
                        "warning",
                        f"Applied batch {batch_id}, but refreshing local data failed. {refresh_error.message}",
                        refresh_error.details,
                    )
            else:
                snapshot = job.result()
                _install_snapshot(workspace, config, snapshot)
                _reset_browse_cache(restart=True)
                switched = st.session_state.pop("browse_switch_notice", None)
                st.session_state.job_notice = switched or ("success", (
                    "Health refreshed; issue pages are read from the warehouse."
                    if st.session_state.browsing else "Local data synchronized."), "")
        except Exception as error:  # noqa: BLE001 - patches must survive worker failures.
            failure = classify(error)
            if st.session_state.job_kind == "export":
                _invalidate_export()
            if (config.browse_mode == "auto" and st.session_state.job_kind == "sync"
                    and not st.session_state.browsing and isinstance(failure, CacheLimitExceeded)):
                # Too many issues for a local snapshot: browse them in the warehouse instead.
                st.session_state.browsing = True
                st.session_state.browse_switch = True
                st.session_state.browse_switch_notice = st.session_state.job_notice = (
                    "info",
                    (
                        "This project has more issues than the local snapshot limits, so issues "
                        "are read from the warehouse one page at a time."
                    ),
                    failure.message,
                )
            else:
                logger.warning(
                    "Background %s job failed: %s", st.session_state.job_kind, failure.log_reason()
                )
                st.session_state.job_notice = ("error", failure.message, failure.details)
        finally:
            st.session_state.job = None
            st.session_state.job_kind = None
            st.session_state.job_patches = []
            st.rerun()

    busy = st.session_state.job is not None
    try:
        check_cache_size(*workspace.cache_size(),
                         MAX_CACHE_ISSUES if st.session_state.browsing else config.max_cache_issues,
                         MAX_CACHE_MIB * 2**20 if st.session_state.browsing else config.max_cache_bytes)
        oversized = False
    except CacheLimitExceeded:
        oversized = True
    pending = [] if oversized else workspace.pending()
    drifted = workspace.drifted_patches() if not busy and not oversized else []
    with st.container(key="dqm-action-bar"):
        col_sync, col_apply, col_state = st.columns([1, 1.35, 3])
        with col_sync:
            if st.button("Sync", disabled=busy):
                _submit_sync(executor, config)
                st.rerun()
        with col_apply:
            if st.button(
                "Apply and sync",
                disabled=busy
                or not pending
                or (bool(drifted) and not st.session_state.allow_drift_overwrite)
                or st.session_state.get("browse_offline", False),
                type="primary",
            ):
                _submit_apply(executor, config, list(pending))
                st.rerun()
        with col_state:
            if busy:
                st.info(f"Background {st.session_state.job_kind} is running…")
            elif st.session_state.browsing:
                reason = (" (above the local snapshot limits)"
                          if config.browse_mode == "auto" else "")
                st.write(
                    f"Pending changes: **{workspace.pending_count()}** · "
                    f"Browsing the warehouse page by page{reason}"
                )
            else:
                st.write(
                    f"Pending changes: **{workspace.pending_count()}** · "
                    f"Last sync: {format_timestamp(workspace.last_sync(), 'Never')}"
                )

    # Rendered below the action bar so the bar keeps its position in the element tree across
    # reruns; a notice above it shifted the bar and left a stale copy on screen.
    notice = st.session_state.job_notice
    if notice is not None:
        level, message, details = notice
        getattr(st, level)(message)
        if details:
            with st.expander("Technical details"):
                st.code(details, language=None)
        st.session_state.job_notice = None

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
        try:
            if st.session_state.browsing:
                _render_warehouse_issues(workspace, pending, config)
            else:
                _render_issues(workspace, pending, config)
        except CacheLimitExceeded as error:
            st.warning(error.message)
            _render_pending_panel(workspace, config)
    with health_tab:
        _render_health(workspace)
    with missed_tab:
        _render_missed_form(config)


def _install_snapshot(workspace, config, snapshot) -> None:
    if "issues" not in snapshot:  # warehouse mode refreshes Health only
        workspace.replace_health(snapshot["health"], installation_id=snapshot.get("installation_id"))
        return
    workspace.replace_synced_data(snapshot["issues"], snapshot["health"],
                                  installation_id=snapshot.get("installation_id"),
                                  max_issues=config.max_cache_issues,
                                  max_bytes=config.max_cache_bytes)


def _submit_sync(executor, config) -> None:
    _clear_selection()
    _invalidate_export()
    st.session_state.job_kind = "sync"
    worker = browse_refresh_worker if st.session_state.browsing else sync_worker
    st.session_state.job = executor.submit(run_worker, worker, config)


def _submit_apply(executor, config, patches) -> None:
    _invalidate_export()
    st.session_state.job_kind = "apply"
    st.session_state.job_patches = patches
    st.session_state.job = executor.submit(
        run_worker, apply_worker, config, patches, st.session_state.browsing)


BROWSE_STATE = ("browse_options", "browse_summaries", "browse_pages")


@dataclass
class Chunk:
    ids: list[str]
    next_cursor: object
    generation: object


def _reset_browse_cache(restart: bool = False) -> None:
    """Forget warehouse reads so the next render re-reads them. A restart (Sync, or the
    generation-change banner) also returns to the first page and a new generation baseline."""
    for key in BROWSE_STATE:
        st.session_state.pop(key, None)
    if restart:
        _clear_selection()
        _invalidate_export()
        for key in ("browse_cursors", "browse_page_index", "browse_generation", "browse_key",
                    "browse_offline"):
            st.session_state.pop(key, None)


def _warehouse_read(read, *args):
    """Run one warehouse browse read; show a failure instead of raising."""
    try:
        return read(*args)
    except SearchTooCostly:
        raise
    except Exception as error:  # noqa: BLE001 - the UI reports every warehouse failure.
        failure = classify(error)
        logger.warning("Warehouse browse read failed: %s", failure.log_reason())
        st.session_state.browse_failure = (failure.message, failure.details)
        return None


def _render_offline(workspace, pending, config) -> None:
    """The warehouse can't be read: show the bounded cache of recently loaded pages."""
    state = st.session_state
    if not state.get("browse_offline"):
        state.browse_offline = True
        st.rerun()  # disable Apply in the action bar above
    st.warning(
        "Offline: showing recently loaded issues only, as of "
        f"{format_timestamp(workspace.last_page_load(), 'never')} (up to 2,000). Filters, "
        "search and counts apply to these cached issues only. Edits are kept locally; Apply "
        "is available again once the warehouse can be read."
    )
    message, details = state.get("browse_failure", ("", ""))
    if message:
        st.caption(f"Warehouse error: {message}")
    if details:
        with st.expander("Technical details"):
            st.code(details, language=None)
    if st.button("Retry the warehouse"):
        _reset_browse_cache()
        state.pop("browse_offline", None)
        state.pop("browse_failure", None)
        st.rerun()
    _render_issues(workspace, pending, config)


def _status_choices(values) -> list[str]:
    known = [status for status in WORKFLOW_STATUSES if status in set(values)]
    return known + sorted(set(values) - set(known))


def _clear_selection():
    st.session_state.bulk_selection = Selection()
    st.session_state.bulk_reset_controls = True
    for key in list(st.session_state):
        if key.startswith("select-issue-"):
            del st.session_state[key]


def _invalidate_export():
    directory = st.session_state.pop("export_directory", None)
    st.session_state.pop("prepared_export", None)
    if directory is not None:
        directory.cleanup()


def _toggle_selection(path, identity, key):
    selection = st.session_state.bulk_selection
    if st.session_state[key]:
        try:
            rows = Workspace(path).saved_rows_for([identity])
            if not rows:
                raise ValueError("This issue is no longer cached. Sync and select again.")
            selection.add(rows)
        except Exception as error:  # noqa: BLE001 - retain the earlier selection.
            st.session_state[key] = False
            st.session_state.review_notice = classify(error).message
    else:
        selection.remove(identity)


def _selection_checkbox(workspace, row):
    identity = str(row["occurrence_id"])
    key = "select-issue-" + identity
    if key not in st.session_state:
        st.session_state[key] = identity in st.session_state.bulk_selection.rows
    st.checkbox("Select issue", key=key, on_change=_toggle_selection,
                args=(workspace.path, identity, key), disabled=st.session_state.get("browse_offline", False))


def _render_review_tools(workspace, config, page_rows, filters, pending_only,
                         owner_options, population=None, expected=None, stale=False):
    context = (str(workspace.path), filters, pending_only, population, expected)
    previous = st.session_state.get("review_context")
    if st.session_state.get("browse_offline") and previous and previous[0] == str(workspace.path):
        # The fallback renderer changes its filter defaults and data source, not the user's
        # selection. Keep saved bases through connection failures; staging remains disabled.
        context = previous
    if st.session_state.get("review_context") != context:
        _clear_selection()
        _invalidate_export()
        st.session_state.review_context = context
    if st.session_state.pop("bulk_reset_controls", False):
        for key in ("bulk-owner", "bulk-status", "bulk-verdict"):
            st.session_state.pop(key, None)
    selection = st.session_state.bulk_selection
    disabled = (st.session_state.job is not None or st.session_state.get("browse_offline", False) or stale)
    if notice := st.session_state.pop("review_notice", None):
        st.info(notice)
    with st.expander(f"Bulk edit · {len(selection.rows)} selected", expanded=bool(selection.rows)):
        first, clear, _ = st.columns([1, 1, 2])
        if first.button("Select current page", disabled=disabled or not page_rows):
            try:
                ids = [str(row["occurrence_id"]) for row in page_rows]
                saved = workspace.saved_rows_for(ids)
                if len(saved) != len(ids):
                    raise ValueError("Some issues are no longer cached. Sync and select again.")
                selection.add(saved)
                for identity in ids:
                    st.session_state["select-issue-" + identity] = True
                st.rerun()
            except Exception as error:  # noqa: BLE001 - selection is atomic.
                st.warning(classify(error).message)
        if clear.button("Clear selection", disabled=disabled or not selection.rows):
            _clear_selection()
            st.rerun()
        st.caption("Selections stay selected across pages. Filter changes and Sync clear selection. "
                   "Up to 2,000 issues or 32 MiB per selection.")
        owner_col, status_col, verdict_col = st.columns(3)
        noop = "— No change —"
        owner = owner_col.selectbox("Bulk owner", [noop, *owner_options], accept_new_options=True,
                                   key="bulk-owner", disabled=disabled)
        status = status_col.selectbox("Bulk workflow status", [noop, *WORKFLOW_STATUSES],
                                      key="bulk-status", disabled=disabled)
        verdict = verdict_col.selectbox("Bulk review verdict", [noop, *REVIEW_VERDICTS],
                                        key="bulk-verdict", disabled=disabled)
        changes = {}
        if owner != noop:
            changes["dqm_owner"] = owner_value_from_selection(owner)
        if status != noop:
            changes["workflow_status"] = status
        if verdict != noop:
            changes["review_verdict"] = verdict
        if changes:
            labels = {"dqm_owner": "Owner", "workflow_status": "Status", "review_verdict": "Verdict"}
            st.caption(f"Stage for {len(selection.rows)} selected issue(s): " +
                       "; ".join(f"{labels[field]}: {value or 'Unassigned'}" for field, value in changes.items()) +
                       ". Existing pending edits to these fields will be replaced.")
        if st.button("Stage changes", disabled=disabled or not selection.rows or not changes):
            try:
                affected = workspace.stage_bulk(selection.baselines(), changes)
                # A new widget identity forces the browser to initialize all visible editors
                # from the pending overlay. Deleting session-state keys alone lets Streamlit
                # restore the old frontend values and silently undo part of the bulk batch.
                st.session_state.bulk_widget_generation = st.session_state.get("bulk_widget_generation", 0) + 1
                _clear_selection()
                st.session_state.review_notice = f"Staged changes for {affected} issue(s). Use Apply and sync to save."
                st.rerun()
            except Exception as error:  # noqa: BLE001 - no partial staging.
                st.error(classify(error).message)
    with st.expander("Export saved issues"):
        st.caption("All filtered results across pages, using saved values. Pending edits are excluded. "
                   "Up to 50,000 issues and 128 MiB; narrow filters for larger exports.")
        csv_col, excel_col, _ = st.columns([1, 1, 2])
        for column, label, format in [(csv_col, "Prepare CSV", "csv"), (excel_col, "Prepare Excel", "xlsx")]:
            if column.button(label, disabled=disabled or pending_only):
                _invalidate_export()
                directory = tempfile.TemporaryDirectory(prefix="dbt-dqm-export-", dir=workspace.path.parent)
                st.session_state.export_directory = directory
                st.session_state.job_kind = "export"
                st.session_state.job = _get_executor().submit(
                    run_worker, prepare_export, config, workspace.path, filters,
                    directory.name, format, population, expected)
                st.rerun()
        if pending_only:
            st.caption("Disable Pending edits only to export saved results.")
        prepared = st.session_state.get("prepared_export")
        if prepared is not None and not disabled and not pending_only:
            st.caption(f"{prepared.rows:,} issue(s) in the prepared export.")
            with open(prepared.path, "rb") as file:
                st.download_button("Download " + ("CSV" if prepared.format == "csv" else "Excel"), file,
                    file_name="dqm-issues." + prepared.format,
                    mime="text/csv" if prepared.format == "csv" else
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _render_warehouse_issues(workspace, pending, config) -> None:
    """Issues read from the warehouse one page at a time (design: docs/design/warehouse-browsing.md).

    Filters, search, counts and the sort run in the warehouse. Each loaded page is stored in the
    bounded local cache, so edits are staged against it exactly as in snapshot mode.
    """
    state = st.session_state
    if state.get("browse_offline"):  # until Retry or Sync, don't wait on the warehouse again
        _render_offline(workspace, pending, config)
        return
    if "browse_population" not in state:
        state.browse_population = browse_population(config)  # fixed for the session
    population = state.browse_population
    if "browse_options" not in state:
        with st.spinner("Reading filter choices…"):
            options = _warehouse_read(browse_options, config, population)
        if options is None:
            _render_offline(workspace, pending, config)
            return
        state.browse_options = options
    options = state.browse_options

    notice = state.pop("browse_notice", None)
    if notice:
        st.warning(notice)
    metrics_area = st.container()  # filled once the summary is read, above the filters

    lifecycle_col, status_col, owner_col, created_col, search_col = st.columns(
        [0.9, 1.2, 1.45, 1.65, 2]
    )
    with lifecycle_col:
        lifecycle = st.multiselect("Lifecycle", ["Active", "Archived"], default=["Active"],
                                   placeholder="All lifecycles")
    with status_col:
        statuses = st.multiselect("Workflow status", _status_choices(options["statuses"]),
                                  placeholder="All statuses")
    with owner_col:
        owners = st.multiselect("Owner", options["owners"], placeholder="All owners")
    with created_col:
        low, high = calendar_date(options["first_seen_min"]), calendar_date(options["first_seen_max"])
        if low is None or high is None:
            created_range = ()
            st.caption("Created date unavailable")
        else:
            created_range = st.date_input(
                "Created date", value=(low, high), min_value=low, max_value=high,
                format="YYYY-MM-DD",
                help="Show issues created within this inclusive local calendar-date range.",
            )
    with search_col:
        # Search runs only when submitted: in the warehouse every search is a billed query.
        with st.form("dqm-search-form", border=False):
            search_field, search_button = st.columns([3, 1])
            with search_field:
                search_draft = st.text_input(
                    "Search", value=state.get("applied_search", ""),
                    help="3 to 200 characters. Applied when you submit it.",
                )
            with search_button:
                st.markdown("<div style='height: 1.6rem'></div>", unsafe_allow_html=True)
                search_submitted = st.form_submit_button("Apply")
        if search_submitted:
            try:
                normalized_search(search_draft)
                state.applied_search = search_draft
            except InvalidSearch as error:
                st.warning(f"{error} The search wasn't applied.")
        search = state.get("applied_search", "")

    priority_col, tags_col, recurring_col = st.columns(3)
    with priority_col:
        priorities = st.multiselect("Priority", ["critical", "high", "medium", "low", "unset"])
    with tags_col:
        tags = st.multiselect("Tags", options["tags"])
    with recurring_col:
        recurring_only = st.checkbox("Recurrences only")
    pending_only = st.checkbox("Pending edits only", help="Includes edits hidden by other filters.")

    created_from = created_to = None
    if len(created_range) == 2 and tuple(created_range) != (low, high):
        created_from, created_to = created_range
    filters = Filters(
        lifecycle=tuple(lifecycle), statuses=tuple(statuses), owners=tuple(owners),
        created_from=created_from, created_to=created_to, priorities=tuple(priorities),
        tags=tuple(tags), recurrences_only=recurring_only, search=search or None,
    )
    page_size_col, nav_col, page_caption_col = st.columns([1, 1.6, 2.4])
    with page_size_col:
        page_size = st.selectbox("Per page", PAGE_SIZES, index=0, key="dqm-page-size")
    browse_key = (filters, page_size)
    if state.get("browse_key") != browse_key:
        state.browse_key = browse_key
        state.browse_cursors = [None]
        state.browse_page_index = 0
        state.pop("browse_pages", None)
    summaries = state.setdefault("browse_summaries", {})
    pages = state.setdefault("browse_pages", {})  # chunk number -> Chunk (ids, cursor, generation)
    index = state.browse_page_index
    # A chunk is what one warehouse read returns: one page on Postgres, CHUNK_ISSUES on BigQuery
    # (where a read costs the same for 25 rows or 2,000). Pages are slices of a chunk; cursors are
    # kept per chunk.
    chunk_size = CHUNK_ISSUES if config.adapter_type == "bigquery" else page_size
    chunk_number, offset = divmod(index * page_size, chunk_size)

    has_next = False
    stale_generation = False
    if pending_only:
        page_rows = workspace.rows_for(workspace.pending_issue_ids())
        summary = summaries.get(filters)
    else:
        if chunk_number in pages:  # rows trimmed from the local cache since: read them again
            listed = pages[chunk_number].ids[offset:offset + page_size]
            if len(workspace.rows_for(listed)) != len(listed):
                pages.pop(chunk_number)
        try:
            with st.spinner("Reading issues from the warehouse…"):
                if filters not in summaries:
                    summary = _warehouse_read(browse_summary, config, population, filters)
                    if summary is None:
                        _render_offline(workspace, pending, config)
                        return
                    summaries[filters] = summary
                summary = summaries[filters]
                if chunk_number not in pages:
                    page = _warehouse_read(browse_page, config, population, filters,
                                           state.browse_cursors[chunk_number], chunk_size)
                    if page is None:
                        _render_offline(workspace, pending, config)
                        return
                    workspace.upsert_pages(page.rows)
                    pages[chunk_number] = Chunk(
                        [str(row["occurrence_id"]) for row in page.rows], page.next_cursor,
                        page.generation)
        except SearchTooCostly as error:
            # Refused before anything ran: go back to the previous search and its results.
            state.browse_notice = f"Search “{search}” wasn't run. {error.message}"
            state.applied_search = state.get("browse_last_search", "")
            summaries.pop(filters, None)
            st.rerun()
        state.browse_last_search = search
        page = pages[chunk_number]
        if state.get("browse_generation") is None:
            state.browse_generation = page.generation
        if page.generation != state.browse_generation or summary["generation"] != page.generation:
            stale_generation = True
            st.warning(
                "The warehouse changed since you started browsing (a reconciliation finished). "
                "Restart from the first page to see consistent results."
            )
            if st.button("Restart from first page"):
                _reset_browse_cache(restart=True)
                st.rerun()
        page_rows = workspace.rows_for(page.ids[offset:offset + page_size])
        if page.next_cursor is not None and len(state.browse_cursors) == chunk_number + 1:
            state.browse_cursors.append(page.next_cursor)
        has_next = offset + page_size < len(page.ids) or page.next_cursor is not None

    with metrics_area:
        metric_active, metric_new, metric_blocked, metric_local, metric_recurring = st.columns(5)
    if summary is not None:
        metric_active.metric("Active issues", summary["active_count"])
        metric_new.metric("New", summary["new_count"])
        metric_blocked.metric("Blocked", summary["blocked_count"])
        metric_recurring.metric("Recurring", summary["recurring_count"])
    metric_local.metric("Pending edits", len(pending))

    with nav_col:
        st.markdown("<div style='height: 1.6rem'></div>", unsafe_allow_html=True)
        first_col, previous_col, next_col = st.columns(3)
        has_next = not pending_only and has_next
        if first_col.button("First", disabled=pending_only or index == 0):
            state.browse_page_index = 0
            st.rerun()
        if previous_col.button("Previous", disabled=pending_only or index == 0):
            state.browse_page_index = index - 1
            st.rerun()
        if next_col.button("Next", disabled=not has_next):
            state.browse_page_index = index + 1
            st.rerun()
    with page_caption_col:
        st.markdown("<div style='height: 1.6rem'></div>", unsafe_allow_html=True)
        if pending_only:
            st.caption(f"Showing {len(page_rows)} issue(s) with pending edits")
        else:
            st.caption(f"Page {index + 1} · showing {len(page_rows)} of "
                       f"{summary['matching']:,} matching issue record(s)")

    owners_for_editor = [*options["owners"], *(row.get("dqm_owner") for row in page_rows)]
    editor_owner_options = owner_editor_options(
        [owner for owner in owners_for_editor if owner != "Unassigned"])
    _render_review_tools(workspace, config, page_rows, filters, pending_only, editor_owner_options,
                         population, state.get("browse_generation"), stale_generation)
    staged_change = False
    for row in page_rows:
        widget_prefix = f"{row['occurrence_id']}-{row.get('annotation_version')}-w-{st.session_state.get('bulk_widget_generation', 0)}"
        staged_change |= _render_issue_card(workspace, row, widget_prefix, editor_owner_options)
    if staged_change:
        st.rerun()
    if workspace.pending():
        st.warning("There are unapplied local changes. Apply them before closing the application.")


def _render_pending_panel(workspace, config) -> None:
    """Bounded escape hatch for old oversized caches, without decoding cached payloads."""
    count = workspace.pending_issue_count()
    st.subheader("Pending edits")
    if not count:
        st.info("No pending edits. Reduce the archive window and restart the app to sync.")
        return
    pages = max(1, math.ceil(count / 100))
    key = "pending-issue-page"
    if key in st.session_state:
        st.session_state[key] = min(st.session_state[key], pages)
    page = st.number_input("Pending edits page", min_value=1, max_value=pages, value=1,
                           step=1, key=key)
    st.caption(f"Up to 100 issues per page · {count:,} issues with pending edits")
    try:
        patches = workspace.pending_page(page, max_bytes=config.max_cache_bytes)
    except CacheLimitExceeded as error:
        st.error(error.message)
        previews = workspace.pending_previews(page)
        for patch in previews:
            st.write(f"{patch.occurrence_id} · {patch.field_name} (preview only)")
            st.code(patch.new_value or "(unset)", language=None)
        if st.button("Discard oversized edits on this page"):
            workspace.discard_patches(previews)
            st.rerun()
        return
    for patch in patches:
        st.write(f"{patch.occurrence_id} · {patch.field_name}")
        st.code((patch.new_value or "(unset)")[:4000], language=None)
    if st.button("Apply this page", disabled=st.session_state.job is not None):
        _submit_apply(_get_executor(), config, patches)
        st.rerun()
    if st.button("Discard edits on this page", disabled=st.session_state.job is not None):
        workspace.discard_patches(patches)
        st.rerun()


def _render_issues(workspace, pending, config) -> None:
    rows = workspace.rows(max_issues=config.max_cache_issues, max_bytes=config.max_cache_bytes)
    if not rows:
        st.info("No local data. Select Sync after building the dbt-dqm package models.")
        _render_review_tools(workspace, config, [], Filters(lifecycle=()), False,
                             owner_editor_options([]), expected=workspace.last_sync())
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
    if "dqm_owner" not in frame:
        frame["dqm_owner"] = None
    frame["owner"] = frame["dqm_owner"].map(owner_label)
    editor_owner_options = owner_editor_options(frame["dqm_owner"])

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
    created_from = created_to = None
    if len(created_range) == 2 and tuple(created_range) != (created_dates.min(), created_dates.max()):
        created_from, created_to = created_range
    filters = Filters(lifecycle=tuple(lifecycle), statuses=tuple(statuses), owners=tuple(owners),
                      created_from=created_from, created_to=created_to, priorities=tuple(priorities),
                      tags=tuple(tags), recurrences_only=recurring_only, search=search or None)
    visible = frame.loc[frame.apply(lambda row: matches_filters(row, filters), axis=1)]
    if pending_only:
        visible = frame[frame["_dirty"]]
    visible = visible.copy()
    visible["_priority"] = visible.get(
        "test_priority", pd.Series(index=visible.index, dtype=object)
    ).map(priority_rank)
    visible["_created_sort"] = pd.to_datetime(visible.get("first_seen_at"), errors="coerce")
    visible = visible.sort_values(
        ["_priority", "_created_sort", "occurrence_id"],
        ascending=[True, True, True],
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
    _render_review_tools(workspace, config, page_rows.to_dict("records"), filters, pending_only,
                         editor_owner_options, expected=workspace.last_sync())
    staged_change = False
    sync_token = workspace.last_sync() or "never"
    for _, row in page_rows.iterrows():
        widget_prefix = f"{row['occurrence_id']}-{sync_token}-{st.session_state.get('bulk_widget_generation', 0)}"
        staged_change |= _render_issue_card(workspace, row, widget_prefix, editor_owner_options)

    if staged_change:
        st.rerun()

    if workspace.pending():
        st.warning("There are unapplied local changes. Apply them before closing the application.")


def _render_issue_card(workspace, row, widget_prefix, editor_owner_options) -> bool:
    """One issue card with its editors; returns whether an edit was staged."""
    staged = False
    occurrence_id = str(row["occurrence_id"])
    test_name = str(row.get("test_name") or "Unnamed test")
    with st.container(border=True):
        _selection_checkbox(workspace, row)
        title_col, owner_col, workflow_col = st.columns([4.5, 2, 1.5])
        with title_col:
            st.subheader(test_name)
            state_text = str(row.get("record_status") or "Unknown")
            if bool(row.get("_dirty")):
                state_text += " · Unsaved local changes"
            st.caption(state_text)
        with owner_col:
            current_owner = owner_value_from_selection(row.get("dqm_owner"))
            owner_selection = st.selectbox(
                "Owner",
                editor_owner_options,
                index=editor_owner_options.index(
                    current_owner if current_owner is not None else "— Unassigned —"
                ),
                key=f"{widget_prefix}-dqm_owner",
                accept_new_options=True,
                placeholder="Select or type an owner",
                help="Choose an existing owner or type a new one.",
            )
            dqm_owner = owner_value_from_selection(owner_selection)
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
            "dqm_owner": dqm_owner,
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
                staged = True
    return staged


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
    st.caption("Limited collections reached the raw-row limit: total failures are unknown, and omitted issues remain active.")
    st.caption(
        "Area totals overlap: a test belongs to each direct model/source dependency. Known misses are reported observations, not a false-negative rate."
    )
    st.subheader("Maintenance")
    maintenance = maintenance_table(health.get("maintenance"))
    if maintenance is None:
        st.info("Maintenance health needs the current package. Run dbt-dqm run, then sync.")
    elif not maintenance:
        st.info("No optional maintenance is configured for this warehouse.")
    else:
        st.dataframe(pd.DataFrame(maintenance), hide_index=True)
        st.caption(
            "A SQL error in a maintenance step doesn't fail the dbt run; the step is retried next "
            "time. OK means the latest assessed run succeeded; unknown means an outcome is "
            "missing. Look up a diagnostic id in BigQuery job history or as a Postgres SQLSTATE."
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
            failure = classify(error)
            logger.warning("Missed-issue submission failed: %s", failure.details or failure.message)
            st.error(f"Report could not be confirmed; retry uses the same ID. {failure.message}")
            with st.expander("Technical details"):
                st.code(failure.details, language=None)
        else:
            st.success("Missed issue recorded. Synchronize to refresh health reporting.")
            st.session_state.missed_submission_id = str(uuid.uuid4())


if __name__ == "__main__":
    run_app()

"""Warehouse browsing checks shared by the Postgres and BigQuery acceptance suites.

They run against the real demo views (column types, JSON tags, timestamps), complementing the
synthetic equivalence tests in tests/test_browse.py.
"""

from __future__ import annotations

from dataclasses import replace

from dbt_dqm_app.browse import Filters
from dbt_dqm_app.display import tag_list
from dbt_dqm_app.warehouse import browse_page, browse_population, browse_summary, fetch_issues


def check_browsing(config, lifecycle="Active"):
    """Every filter and the summary agree with the verified snapshot read on the same data."""
    issues = fetch_issues(config)
    active = [row for row in issues if row["record_status"] == lifecycle]
    assert active, f"the demo should have {lifecycle} issues"
    population = browse_population(config)

    base = Filters(lifecycle=(lifecycle,))
    page = browse_page(config, population, base, None, 25)
    assert len(active) <= 25 and page.next_cursor is None
    assert [row["occurrence_id"] for row in page.rows] == sorted_ids(active)
    assert {key for row in page.rows for key in row} == {key for row in issues for key in row}

    summary = browse_summary(config, population, base)
    assert summary["matching"] == len(active)
    assert summary["active_count"] == sum(row["record_status"] == "Active" for row in issues)
    assert summary["generation"] == page.generation

    first = active[0]
    cases = {
        replace(base, search=first["test_name"]): [r for r in active if r["test_name"] == first["test_name"]],
        replace(base, priorities=(first["test_priority"] or "unset",)): [
            r for r in active if (r["test_priority"] or "unset") == (first["test_priority"] or "unset")
        ],
        Filters(lifecycle=()): issues,
    }
    tags = tag_list(first["test_tags"])
    if tags:
        cases[replace(base, tags=(tags[0],))] = [r for r in active if tags[0] in tag_list(r["test_tags"])]
    for filters, expected in cases.items():
        got = browse_page(config, population, filters, None, 200).rows
        assert {row["occurrence_id"] for row in got} == {row["occurrence_id"] for row in expected}, filters
        assert browse_summary(config, population, filters)["matching"] == len(expected), filters
    return page.generation


def sorted_ids(rows):
    from dbt_dqm_app.display import priority_rank

    statuses = ("NEW", "BLOCKED", "IN_PROGRESS", "TRIAGED", "RESOLVED", "ACCEPTED_RISK",
                "FALSE_POSITIVE")
    ranks = {status: rank for rank, status in enumerate(statuses)}
    return [row["occurrence_id"] for row in sorted(rows, key=lambda row: (
        priority_rank(row["test_priority"]), ranks.get((row["workflow_status"] or "NEW").upper(), 99),
        row["first_seen_at"], row["occurrence_id"],
    ))]


def check_page_edit(config, workspace_path):
    """An edit staged from a warehouse page applies with compare-and-set; a stale one doesn't."""
    import pytest

    from dbt_dqm_app.store import Workspace
    from dbt_dqm_app.warehouse import apply_patches

    population = browse_population(config)
    workspace = Workspace(workspace_path)
    page = browse_page(config, population, Filters(), None, 25)
    workspace.upsert_pages(page.rows)
    target = page.rows[0]["occurrence_id"]
    workspace.set_change(target, "notes", "edited from a warehouse page")
    apply_patches(config, workspace.pending())
    workspace.clear_applied(workspace.pending())
    reread = browse_page(config, population, Filters(), None, 25)
    row = next(r for r in reread.rows if r["occurrence_id"] == target)
    assert row["notes"] == "edited from a warehouse page"
    assert row["annotation_version"] == page.rows[0]["annotation_version"] + 1
    # A second reviewer staged against the page loaded before that apply: rejected, not merged.
    stale = Workspace(workspace_path.with_name("stale.sqlite"))
    stale.upsert_pages(page.rows)
    stale.set_change(target, "notes", "stale overwrite")
    with pytest.raises(Exception, match="(?i)annotation conflict"):
        apply_patches(config, stale.pending())
    final = browse_page(config, population, Filters(), None, 25)
    assert next(r for r in final.rows if r["occurrence_id"] == target)["notes"] == (
        "edited from a warehouse page")

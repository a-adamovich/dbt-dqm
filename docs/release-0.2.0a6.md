# 0.2.0a6 release verification

This source is prepared for a compatible follow-up to a5. Warehouse contract `dqm-full-v1` remains
unchanged. Python/application/lock version: `0.2.0a6`; dbt package: `0.2.0-a6`.
Upgrades from a4/a5 require no history reset. Scheduled full refreshes now explicitly require `--yes`.

The selected changes are full-refresh preview/confirmation, bounded cross-page bulk editing,
and background CSV/Excel export of filtered saved issues. Excel expands only the top-level
`record_values_json` fields. Other feedback items and performance redesign are excluded; maintenance
shortcuts remain opt-in.

All release-checklist gates must pass before the tag is pushed. Candidate revision, complete CI,
warehouse, permissions, installed-wheel browser and export-enabled memory results are recorded in
the preparation PR and on the [v0.2.0a6 release page](https://github.com/a-adamovich/dbt-dqm/releases/tag/v0.2.0a6).
The final publication record there includes the merged source revision, reviewer pause/approval,
trusted-publishing run, artifact hashes/provenance, public installation combinations and Git-tag
installation/parse/offline compilation. This file is a prepublication record; the linked final
verification supersedes preparation statuses.

# Pilot walkthrough: one test, one issue, one full loop

This walkthrough installs dbt-dqm and tracks one existing dbt test. You review the first issue,
then watch it pass and recur. It takes about half an hour.

**Use a development target throughout.** The pass-and-recur steps edit data on purpose, so use a
small synthetic model whose bad row you control. Don't use production data or a production
schema.

## 1. Install

From your dbt project, add the package to `packages.yml` and run `dbt deps`:

```yaml
packages:
  - git: https://github.com/a-adamovich/dbt-dqm.git
    revision: v0.2.5
```

Then install the command in the same Python environment as your engine. For example, for Postgres:

```bash
python -m pip install 'dbt-core==1.12.6' 'dbt-postgres==1.11.0'
python -m pip install 'dbt-dqm[postgres]==0.2.5'
```

The [README](../README.md#get-started) has the BigQuery commands. Add retention settings to
`dbt_project.yml`:

```yaml
vars:
  dbt_dqm_retention_days: 30
  dbt_dqm_event_retention_days: 90
```

## 2. Track one existing test

Opting a test in adds `enabled: "{{ var('dqm_enabled', false) }}"`, and **that line turns the test
off in ordinary `dbt test` and `dbt build` runs**. Only `dbt-dqm run` turns it on.

If the test must keep running in your normal CI, leave it unchanged and add a separately named copy
for the pilot. This example tracks a `not_null` test on a synthetic model, `pilot_customers`, that
returns one row with a null email:

```yaml
models:
  - name: pilot_customers
    columns:
      - name: email
        data_tests:
          - not_null                  # the existing test, unchanged
          - not_null:                 # the DQM pilot copy
              name: dqm_pilot_email_not_null
              config:
                enabled: "{{ var('dqm_enabled', false) }}"
                store_failures: true
                limit: 100
                meta:
                  dbt_dqm:
                    granularity: ['customer_id']   # the columns that identify one issue
                    owner: pilot_team
```

`dbt ls --resource-type test` now lists only the original test. Build the model, then run the pilot
test:

```bash
dbt run --select pilot_customers
dbt-dqm run --select dqm_pilot_email_not_null --summary
```

**Use the same `--select dqm_pilot_email_not_null` on every pilot run**, so the summary counts come
from this test alone. The first run creates the tracking tables and exits with code `1`, because
the test failed. That is expected: the failure was captured. The summary shows `OPENED (NEW)` 1.

## 3. Review the first issue

```bash
dbt-dqm app
```

In the browser app:

1. Sync.
2. Open the issue card for `customer_id` 2.
3. Set the owner if needed, choose a review verdict and add a note.
4. Choose **Apply and sync** to save the edits to the warehouse.

## 4. See it pass

Fix the synthetic row so the email is no longer null, then rebuild and rerun:

```bash
dbt run --select pilot_customers
dbt-dqm run --select dqm_pilot_email_not_null --summary
```

The command exits `0` and the summary shows `ARCHIVED` 1. In the app, after you sync, the issue is
Archived with close reason Passed. It keeps your verdict and note. Include archived issues in the
lifecycle filter to see it.

## 5. See it recur

Put the null email back, then rebuild and rerun with the same selector. The command exits `1` and the
summary shows `RECURRING` 1.

The app shows a new active issue for the same `customer_id`:

- It is occurrence 2, marked as a recurrence, with status NEW and verdict UNREVIEWED.
- The earlier verdict, note and close reason are shown with it as the previous occurrence's
  values.
- Occurrence 1 stays archived with its own review intact.

## What you have seen

- One opt-in configuration turned a dbt test into tracked issues, identified by `customer_id`.
- A review saved in the app survived the issue passing.
- When the problem returned, it was recognized as the same record recurring, not a brand-new
  issue, with the earlier review available alongside it.

Next, see [configure tests and run DQM](using-dbt-dqm.md) for owners, limits and selection, and the
[review app guide](review-app.md) for bulk edits and exports.

This walkthrough was checked on 2026-11-11 with the published `dbt-dqm` 0.2.5, dbt Core 1.12.6 and
Postgres 16, using a synthetic model and a disposable database. The review edit was applied through
the app's own `apply_patches`. The browser clicks themselves were not exercised in that check.

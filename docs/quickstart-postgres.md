# Quickstart on disposable Postgres

Run the shipped [Postgres demo](../integration_tests/demo_postgres) with synthetic data. It exercises three data tests with one-, two-, and three-column identities.

## 1. Start Postgres

**Ran.** From a shell with Docker and uv, use a fresh checkout of this repository and start a dedicated container. The example uses the container name `dbt-dqm-demo`. If it is taken, use a different name throughout. Docker assigns a free host port on loopback.

```bash
docker run --rm -d --name dbt-dqm-demo \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=dbt_dqm_local \
  -e POSTGRES_DB=dbt_dqm_demo -p 127.0.0.1::5432 postgres:16
docker port dbt-dqm-demo 5432/tcp
```

Check for readiness. Repeat the check until the server reports that it is accepting connections
over TCP inside the container.

```bash
docker exec dbt-dqm-demo pg_isready -h 127.0.0.1 -U postgres -d dbt_dqm_demo
```

Then install `pgcrypto` for the `digest()` function used to hash identities.

```bash
docker exec dbt-dqm-demo psql -v ON_ERROR_STOP=1 -U postgres -d dbt_dqm_demo \
  -c 'create extension if not exists pgcrypto'
```

## 2. Install and seed the demo

**Ran.** From the checkout root, install the locked Core environment. Copy the shipped profile to a temporary directory outside the checkout.

```bash
uv sync --locked --group dbt1 --extra postgres
source .venv/bin/activate
cd integration_tests/demo_postgres
export DBT_PROFILES_DIR="$(mktemp -d)"
cp profiles.yml.example "$DBT_PROFILES_DIR/profiles.yml"
```

In the copied profile, set the port to the one returned by `docker port`, the user to `postgres`, and the password to `dbt_dqm_local`. Keep the shipped database and schema at `dbt_dqm_demo`. Run the following from the demo directory. The reset macro drops demo tracking objects in this target, so use it only with the disposable database.

```bash
dbt deps
dbt run-operation reset_demo_dqm
dbt seed
```

## 3. Open and review issues

**Ran.** The shell function runs one scenario and checks its assertions. The explicit
`dqm_enabled` variable makes the demo assertion selectable by ordinary dbt.

```bash
run_demo() {
  dbt run --select demo_records --vars "{demo_scenario: $1}" &&
  dbt-dqm run --vars "{demo_scenario: $1}" --summary &&
  dbt test --select assert_scenario_outcomes \
    --vars "{demo_scenario: $1, dqm_enabled: true}"
}
run_demo initial
```

All three commands should exit `0`. The demo's tracked tests use `severity: warn`, so their warnings still allow a successful DQM exit. The summary shows `OPENED (NEW)` at 7. The assertion checks that the three tests have 2, 2, and 3 active occurrences, all numbered 1.

**Ran.** In a second shell, activate the same environment and start the app from the demo directory with the same `DBT_PROFILES_DIR`.

```bash
dbt-dqm app
```

Open the local URL, select **Sync**, and find the customer email issue for `cust-001` (customer IDs differing only in case share an identity). Set **Review verdict** to `TRUE_POSITIVE`. Open **Edit annotations**, add the note `Confirmed invalid email`, then leave the Notes field to stage it. Choose **Apply and sync** and check that the saved values are visible.

## 4. Pass, then recur

**Ran.** Run the pass scenario from the first shell.

```bash
run_demo passed
```

In the browser, select **Sync** and include **Archived** in the lifecycle filter to see
the saved verdict and note. Then run the recurrence scenario from the first shell.

```bash
run_demo recurrence
```

| Scenario | DQM exit | Summary change | Warehouse result |
| --- | ---: | --- | --- |
| `passed` | 0 | ARCHIVED 7 | The seven original occurrences are archived as Passed. |
| `recurrence` | 0 | RECURRING 3 | Each test has one active occurrence numbered 2 and retains its archived history. |

Both assertion runs should exit `0`.

**Ran.** After `recurrence`, select **Sync** again and inspect the active `cust-001` card. Its workflow status is `NEW` and review verdict is `UNREVIEWED`. Expand **Previous investigation** to see the earlier note. The archived card retains the earlier verdict. You can also read the prior verdict from the active row's `previous_review_verdict` in `dqm_all_issues`.

## 5. Tear down

**Ran.** Stop the app, then run these commands from the same demo directory and shell. The workspace purge discards the snapshot and any local drafts for this demo.

```bash
dbt-dqm workspace purge
dbt-dqm workspace info
docker rm -f dbt-dqm-demo
docker ps -a --filter 'name=^/dbt-dqm-demo$'
rm "$DBT_PROFILES_DIR/profiles.yml"
rmdir "$DBT_PROFILES_DIR"
unset DBT_PROFILES_DIR
```

The workspace should report `Exists: no` and the filtered container list should contain no rows. See [CLI reference](reference-cli.md) for exit codes and [Review app workflows](review-app.md) for annotation controls.

# On-demand BigQuery verification

This guide sets up a secure connection from GitHub Actions to your BigQuery project. You only
need it if you want to run the repository's credentialed BigQuery checks **in GitHub Actions**.
It is separate from the credentials you use when running dbt or the review app on your computer.
The workflow is started manually; setting it up does not start weekly jobs.

When started from `main`, `.github/workflows/bigquery-lifecycle.yml`:

1. checks that the CI identity **cannot** read the shared QA dataset (`dbt_dqm_qa`);
2. runs the demo lifecycle for dbt Core 1.12.6 and dbt 2.0.11 in separate
   `dbt_dqm_scheduled_dbt1` and `dbt_dqm_scheduled_dbt2` datasets in the US multi-region. The
   CI identity creates each one on its first run, so it owns them, and reuses them afterwards;
3. runs the acceptance suite in parallel, with a disposable, labelled `dqm_acceptance_*`
   dataset for each test; and
4. sweeps labelled acceptance datasets older than six hours. Do not run other acceptance
   tests that need those old datasets at the same time;
5. after both engines pass, runs the serial `cross-engine-upgrade` job from Core 1.12.6 to
   dbt 2.0.11 on a disposable dataset; and
6. then runs the serial `restricted-permissions` job on another disposable dataset.

Results are uploaded as `scheduled-bigquery-dbt1-results`, `scheduled-bigquery-dbt2-results`,
`cross-engine-bigquery-results` and `restricted-bigquery-results` artifacts. All four jobs must
pass on the exact candidate commit before a release; see the
[release checklist](release-checklist.md).

## How the connection works

1. A manually started GitHub Actions job asks GitHub for a short-lived **OIDC token**. This
   token identifies the repository, workflow, branch and event running the job.
2. The Google Cloud **workload identity provider** verifies that token and accepts it only for
   this repository's numeric ID and owner ID, the exact `bigquery-lifecycle.yml` workflow on
   `refs/heads/main`, and a manual dispatch. The provider belongs to a **workload identity
   pool**, which groups external identities Google Cloud can trust.
3. An IAM binding lets that trusted GitHub identity impersonate a dedicated Google Cloud
   **service account**. The job receives short-lived credentials for that account.
4. The service account's BigQuery roles let the tests run jobs and create their own datasets.

No service-account key file is created, downloaded, or stored in GitHub. Creating a pool, a
provider, and a service account is a one-time setup; running the workflow is a separate action.
The workflow remains disabled until the `BIGQUERY_LIFECYCLE_ENABLED` repository variable is
set to `true`.

## Identities and roles

The workflow uses three dedicated service accounts. None has a key, and none has standing access
to any dataset:

| Identity | Project role | Per-run access, granted by the provisioner | Used by |
| --- | --- | --- | --- |
| `dbt-dqm-ci` (provisioner) | `roles/bigquery.user`, which includes running jobs and creating datasets it then owns | Owns the datasets it creates | `lifecycle`, `cross-engine-upgrade`, and setup and cleanup in `restricted-permissions` |
| `dbt-dqm-ci-runner` | `roles/bigquery.jobUser` | Writer on the disposable dataset and its audit twin | `restricted-permissions` |
| `dbt-dqm-ci-reviewer` | `roles/bigquery.jobUser` | Reader on the disposable dataset, `dataEditor` on the four reviewer tables | `restricted-permissions` |

Don't give any of them a project-level data role (`dataViewer`, `dataEditor`, `dataOwner` or
`admin`). That would let CI read the shared QA dataset, and would let the stale-dataset sweep reach
datasets CI does not own. Don't reuse `dbt-dqm-runner`, `dbt-dqm-reviewer` or the development
account: they have access to shared QA, and the test fails if a CI identity can read it.

**Residual limitation, for maintainer review.** Inside the `restricted-permissions` job all three
identities trust the same federated principal, and each credential file can request a GitHub
token for any of them. So the separation proves what the runner and reviewer identities are
permitted to do; it is not a security boundary between steps of that job. Any code running in
the job, such as a compromised dependency, could act as the provisioner, which can create
datasets, run billed queries and read or delete the datasets it owns, but cannot read the shared
QA dataset. The job turns off automatic credential exports and passes each identity explicitly,
which prevents accidental use but not deliberate misuse. Moving provisioning into a separate job
would not remove this on its own: the restricted job's federated identity would still be allowed
to impersonate the provisioner. It would help only with a separate trust path, for example a
provisioner binding limited to a different job's token subject, so the restricted job could not
obtain provisioner credentials at all. This limitation is open for maintainer review; it has not
been accepted.

## Before you begin

- Use [Google Cloud Shell](https://shell.cloud.google.com/) or a terminal with `gcloud`, signed in
  as a user who can enable services, administer workload identity pools and service accounts, and
  set project IAM policy (for example `roles/serviceusage.serviceUsageAdmin`,
  `roles/iam.workloadIdentityPoolAdmin`, `roles/iam.serviceAccountAdmin` and
  `roles/resourcemanager.projectIamAdmin`). The runner, reviewer and development keys do not have
  these permissions.
- You also need permission to manage Actions variables in the GitHub repository, with the GitHub
  CLI installed and authenticated, or the web UI.
- This setup creates identities and IAM bindings only. It does not change existing accounts,
  bindings or datasets, create keys, reset datasets or start a run. A later manual run uses
  BigQuery and can incur charges; see [BigQuery pricing](https://cloud.google.com/bigquery/pricing).
- Run every block below in the **same shell session**, so the variables remain available.

```bash
PROJECT_ID=dbt-dqm
PROJECT_NUMBER=16757897121
POOL=dbt-dqm-lifecycle
PROVIDER=dbt-dqm
# Numeric IDs survive a rename and cannot be claimed by a re-created repository of the same name.
REPOSITORY_ID=1329204213
OWNER_ID=15641232
WORKFLOW_REF="a-adamovich/dbt-dqm/.github/workflows/bigquery-lifecycle.yml@refs/heads/main"
```

### Pre-setup checks (read-only)

Stop and investigate if any result differs from the expectation in the comment.

```bash
# Expect "true": the IDs above still belong to this repository and project.
test "$(gh api repos/a-adamovich/dbt-dqm --jq '"\(.id) \(.owner.id)"')" = "$REPOSITORY_ID $OWNER_ID" && echo true
test "$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')" = "$PROJECT_NUMBER" && echo true

# Expect BIGQUERY_LIFECYCLE_ENABLED=false and none of the four GCP_* variables.
gh variable list --repo a-adamovich/dbt-dqm

# Expect no dbt-dqm-lifecycle pool, including deleted ones, and none of the three new accounts.
gcloud iam workload-identity-pools list --project="$PROJECT_ID" --location=global --show-deleted
gcloud iam service-accounts list --project="$PROJECT_ID" --format="value(email)"
```

## One-time Google Cloud setup

### 1. Enable the Security Token Service

Federation exchanges GitHub's token through STS. BigQuery, IAM and IAM Credentials must also be
enabled; `gcloud services list --enabled --project="$PROJECT_ID"` shows them.

```bash
gcloud services enable sts.googleapis.com --project="$PROJECT_ID"
```

### 2. Create the dedicated pool and its single provider

The pool holds only this provider, following Google's advice to use one provider per pool to
avoid subject collisions. The condition accepts only a manual dispatch of this one workflow on
`main` in this repository. A pull request, another workflow (including `release.yml`) or another
repository cannot satisfy it merely by knowing the provider name, and adding another workflow does
not extend this trust.

```bash
gcloud iam workload-identity-pools create "$POOL" \
  --project="$PROJECT_ID" --location=global \
  --display-name="dbt-dqm BigQuery lifecycle" \
  --description="Only bigquery-lifecycle.yml on main, by manual dispatch"

gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" \
  --project="$PROJECT_ID" --location=global --workload-identity-pool="$POOL" \
  --display-name="dbt-dqm repository" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository_id=assertion.repository_id,attribute.repository_owner_id=assertion.repository_owner_id,attribute.workflow_ref=assertion.workflow_ref,attribute.event_name=assertion.event_name" \
  --attribute-condition="assertion.repository_id=='$REPOSITORY_ID' && assertion.repository_owner_id=='$OWNER_ID' && assertion.workflow_ref=='$WORKFLOW_REF' && assertion.ref=='refs/heads/main' && assertion.event_name=='workflow_dispatch'"
```

The repository, owner, workflow, branch and event are security restrictions. If you transfer the
repository, rename the workflow or change its branch, update and review the condition first.

### 3. Create the three service accounts

These commands create identities, not keys.

```bash
for name in dbt-dqm-ci dbt-dqm-ci-runner dbt-dqm-ci-reviewer; do
  gcloud iam service-accounts create "$name" --project="$PROJECT_ID" \
    --display-name="$name (GitHub federation only)"
done
```

### 4. Grant the minimum project roles

These add bindings; they do not replace the project's other bindings.

```bash
gcloud projects add-iam-policy-binding "$PROJECT_ID" --condition=None \
  --role=roles/bigquery.user \
  --member="serviceAccount:dbt-dqm-ci@$PROJECT_ID.iam.gserviceaccount.com"

for name in dbt-dqm-ci-runner dbt-dqm-ci-reviewer; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --condition=None \
    --role=roles/bigquery.jobUser \
    --member="serviceAccount:$name@$PROJECT_ID.iam.gserviceaccount.com"
done
```

`roles/bigquery.user` also permits listing dataset and table metadata in the project, but not
reading data in datasets the account does not own. The workflow checks that the provisioner cannot
read the shared QA dataset before running the suite.

### 5. Let the trusted GitHub identity use each account

Authentication alone does not let a token act as a service account. This grants
`roles/iam.workloadIdentityUser` on each of the three accounts, never at project level, to tokens
the provider accepted for this repository ID.

```bash
for name in dbt-dqm-ci dbt-dqm-ci-runner dbt-dqm-ci-reviewer; do
  gcloud iam service-accounts add-iam-policy-binding \
    "$name@$PROJECT_ID.iam.gserviceaccount.com" --project="$PROJECT_ID" --condition=None \
    --role=roles/iam.workloadIdentityUser \
    --member="principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$POOL/attribute.repository_id/$REPOSITORY_ID"
done
```

### 6. Verify (read-only)

Expect the provider `ACTIVE` with exactly the condition above; `dbt-dqm-ci` with only
`roles/bigquery.user`, the runner and reviewer with only `roles/bigquery.jobUser`; existing
accounts unchanged; one `workloadIdentityUser` binding and no user-managed keys on each new account.

```bash
gcloud iam workload-identity-pools providers describe "$PROVIDER" --project="$PROJECT_ID" \
  --location=global --workload-identity-pool="$POOL" --format="yaml(name,state,attributeCondition)"
gcloud projects get-iam-policy "$PROJECT_ID" --flatten="bindings[].members" \
  --filter="bindings.members~^serviceAccount:dbt-dqm" --format="table(bindings.role,bindings.members)"
for name in dbt-dqm-ci dbt-dqm-ci-runner dbt-dqm-ci-reviewer; do
  echo "== $name"
  gcloud iam service-accounts get-iam-policy "$name@$PROJECT_ID.iam.gserviceaccount.com" \
    --format="table(bindings.role,bindings.members)"
  gcloud iam service-accounts keys list --iam-account="$name@$PROJECT_ID.iam.gserviceaccount.com" \
    --managed-by=user
done
```

## Configure GitHub repository variables

These are configuration, not passwords or private keys. Never paste a key into them. Leave
`BIGQUERY_LIFECYCLE_ENABLED` at `false` here; enabling is a separate step below.

```bash
# Only if it is not already set: the project in which the workflow creates its test datasets.
gh variable set DBT_DQM_BIGQUERY_PROJECT --repo a-adamovich/dbt-dqm --body "$PROJECT_ID"

gh variable set GCP_WORKLOAD_IDENTITY_PROVIDER --repo a-adamovich/dbt-dqm \
  --body "projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$POOL/providers/$PROVIDER"
gh variable set GCP_CI_SERVICE_ACCOUNT --repo a-adamovich/dbt-dqm \
  --body "dbt-dqm-ci@$PROJECT_ID.iam.gserviceaccount.com"
gh variable set GCP_CI_RUNNER_SERVICE_ACCOUNT --repo a-adamovich/dbt-dqm \
  --body "dbt-dqm-ci-runner@$PROJECT_ID.iam.gserviceaccount.com"
gh variable set GCP_CI_REVIEWER_SERVICE_ACCOUNT --repo a-adamovich/dbt-dqm \
  --body "dbt-dqm-ci-reviewer@$PROJECT_ID.iam.gserviceaccount.com"
```

## Start and inspect a manual run

The workflow must already be present on `main`; a workflow that exists only on a pull-request
branch cannot satisfy the release gate. A run on a commit that later changes is an infrastructure
proof, not release evidence: release evidence must run on the exact candidate commit.

```bash
# Permit manual runs. This does not start a run or enable a schedule.
gh variable set BIGQUERY_LIFECYCLE_ENABLED --repo a-adamovich/dbt-dqm --body true

# Start the workflow on main, then find and watch the run.
gh workflow run bigquery-lifecycle.yml --repo a-adamovich/dbt-dqm --ref main
gh run list --repo a-adamovich/dbt-dqm --workflow bigquery-lifecycle.yml --limit 5
gh run watch <RUN_ID> --repo a-adamovich/dbt-dqm

# Afterwards, make the workflow inert again.
gh variable set BIGQUERY_LIFECYCLE_ENABLED --repo a-adamovich/dbt-dqm --body false
```

If GitHub shows the jobs as skipped, confirm the enable variable is exactly `true` and the
selected branch is `main`. Authentication errors may indicate an incorrect provider name, a
token that does not satisfy the condition, missing bindings, or a disabled API. The old
`DBT_DQM_BIGQUERY_KEYFILE_JSON` secret is not used by this workflow.

## Rollback

Choose the case that applies. Both use the shell variables from [Before you begin](#before-you-begin).

### A. Undo setup before any run

Nothing has used the identities, so only the setup itself is removed. Remove the project grants
before deleting the accounts; otherwise they remain as `deleted:serviceAccount:` entries.

```bash
gh variable set BIGQUERY_LIFECYCLE_ENABLED --repo a-adamovich/dbt-dqm --body false
for v in GCP_WORKLOAD_IDENTITY_PROVIDER GCP_CI_SERVICE_ACCOUNT GCP_CI_RUNNER_SERVICE_ACCOUNT GCP_CI_REVIEWER_SERVICE_ACCOUNT; do
  gh variable delete "$v" --repo a-adamovich/dbt-dqm
done

gcloud projects remove-iam-policy-binding "$PROJECT_ID" --condition=None \
  --role=roles/bigquery.user \
  --member="serviceAccount:dbt-dqm-ci@$PROJECT_ID.iam.gserviceaccount.com"
for name in dbt-dqm-ci-runner dbt-dqm-ci-reviewer; do
  gcloud projects remove-iam-policy-binding "$PROJECT_ID" --condition=None \
    --role=roles/bigquery.jobUser \
    --member="serviceAccount:$name@$PROJECT_ID.iam.gserviceaccount.com"
done

# Deleting an account also removes the federation binding on it.
for name in dbt-dqm-ci dbt-dqm-ci-runner dbt-dqm-ci-reviewer; do
  gcloud iam service-accounts delete "$name@$PROJECT_ID.iam.gserviceaccount.com" \
    --project="$PROJECT_ID" --quiet
done
gcloud iam workload-identity-pools providers delete "$PROVIDER" --project="$PROJECT_ID" \
  --location=global --workload-identity-pool="$POOL" --quiet
gcloud iam workload-identity-pools delete "$POOL" --project="$PROJECT_ID" --location=global --quiet
```

Deleted pools, providers and accounts keep their names for about 30 days and can be restored
(undeleted) in that window; re-creating them under the same names fails until then.

### B. Clean up after a run, then undo setup

Do these steps in order. Identify the run's datasets **before** deleting the identities: once an
account is deleted, its ownership entries no longer show which datasets it created.

1. Stop new runs and confirm nothing is still running.

   ```bash
   gh variable set BIGQUERY_LIFECYCLE_ENABLED --repo a-adamovich/dbt-dqm --body false
   # Expect no queued or in-progress runs.
   gh run list --repo a-adamovich/dbt-dqm --workflow bigquery-lifecycle.yml --status queued
   gh run list --repo a-adamovich/dbt-dqm --workflow bigquery-lifecycle.yml --status in_progress
   # Expect no pending or running BigQuery jobs from the three CI accounts.
   bq ls -j -a -n 1000 --project_id="$PROJECT_ID" --filter="states:pending,running"
   ```

2. Identify the datasets the run created. List by name prefix, then check every candidate. A
   dataset belongs to this workflow only if all of these hold:
   - It is `dbt_dqm_scheduled_dbt1` or `dbt_dqm_scheduled_dbt2`, or a `dqm_acceptance_*` dataset
     carrying the `dbt_dqm_run` label, or the `_dbt_test__audit` twin of a qualifying dataset.
   - Its explicit owner is `dbt-dqm-ci`. The project-owners group is an owner of every dataset and
     does not count.
   - It was created after the first run started.

   Local acceptance runs use the same prefix and label, so the label alone does not prove a
   dataset is CI's. Errors are printed, never hidden: a failed listing or metadata read means the
   inventory is incomplete, not empty. Leave anything you cannot confirm alone.

   ```bash
   if listing=$(bq ls --project_id="$PROJECT_ID" --max_results=10000 --format=prettyjson); then
     candidates=$(printf '%s' "$listing" | jq -r '.[].datasetReference.datasetId
       | select(startswith("dqm_acceptance_") or startswith("dbt_dqm_scheduled_"))')
     echo "Candidates: ${candidates:-none}"
     printf '%s\n' "$candidates" | while IFS= read -r ds; do
       [ -n "$ds" ] || continue
       echo "== $ds"
       if metadata=$(bq show --format=prettyjson "$PROJECT_ID:$ds"); then
         printf '%s' "$metadata" | jq -c '{creationTime, labels,
           owners: [.access[] | select(.role=="OWNER") | (.userByEmail // .specialGroup)]}'
       else
         echo "Metadata read FAILED for $ds: resolve before deciding." >&2
       fi
     done
   else
     echo "Dataset listing FAILED: this is not an empty inventory." >&2
   fi
   ```

3. Delete each confirmed dataset, one by one.

   ```bash
   bq rm -r -f -d "$PROJECT_ID:<dataset>"
   ```

4. Undo the setup with case A.

### Optional: disable STS

STS was not enabled before this setup. Disabling it is optional, and only safe if nothing else in
the project uses federation. Check first; if anything is listed or you are unsure, leave it
enabled. It has no cost of its own.

```bash
# Expect no pools other than deleted dbt-dqm-lifecycle.
gcloud iam workload-identity-pools list --project="$PROJECT_ID" --location=global --show-deleted
# If the project belongs to an organization, also expect no workforce pools that rely on it.
gcloud iam workforce-pools list --organization=<ORGANIZATION_ID> --location=global

# Without --force, this refuses if another enabled service depends on STS.
gcloud services disable sts.googleapis.com --project="$PROJECT_ID"
```

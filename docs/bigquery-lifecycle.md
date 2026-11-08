# On-demand BigQuery verification

This guide sets up a secure connection from GitHub Actions to your BigQuery project. You only
need it if you want to run the repository's credentialed BigQuery checks **in GitHub Actions**.
It is separate from the credentials you use when running dbt or the review app on your computer.
The workflow is started manually; setting it up does not start weekly jobs.

When started from `main`, `.github/workflows/bigquery-lifecycle.yml`:

1. checks that the CI identity **cannot** read the shared QA dataset (`dbt_dqm_qa`);
2. runs the demo lifecycle for dbt Core 1.12.6 and dbt 2.0.11 in separate
   `dbt_dqm_scheduled_dbt1` and `dbt_dqm_scheduled_dbt2` datasets;
3. runs the acceptance suite in parallel, with a disposable, labelled `dqm_acceptance_*`
   dataset for each test; and
4. sweeps labelled acceptance datasets older than six hours. Do not run other acceptance
   tests that need those old datasets at the same time.

Results are uploaded as `scheduled-bigquery-dbt1-results` and
`scheduled-bigquery-dbt2-results` artifacts. Both matrix jobs must pass on the exact candidate
commit before the 0.2.2 release; see the [compatibility gate](dbt-2-compatibility.md#release-gate).
A failure opens or updates an issue labelled `scheduled-bigquery`.

## How the connection works

1. A manually started GitHub Actions job asks GitHub for a short-lived **OIDC token**. This
   token identifies the repository and branch running the job.
2. The Google Cloud **workload identity provider** verifies that token and accepts it only for
   `a-adamovich/dbt-dqm` on `refs/heads/main`. The provider belongs to a **workload identity
   pool**, which groups external identities Google Cloud can trust.
3. An IAM binding lets that trusted GitHub identity impersonate a dedicated Google Cloud
   **service account**. The job receives short-lived credentials for that account.
4. The service account's BigQuery roles let the tests run jobs and create their own datasets.

No service-account key file is created, downloaded, or stored in GitHub. Creating a pool, a
provider, and a service account is a one-time setup; running the workflow is a separate action.
The workflow remains disabled until the `BIGQUERY_LIFECYCLE_ENABLED` repository variable is
set to `true`.

## Before you begin

- Use [Google Cloud Shell](https://shell.cloud.google.com/) or a terminal with `gcloud` installed
  and authenticated as a Google Cloud user who can manage workload identity pools, service
  accounts, and IAM policy bindings in the `dbt-dqm` project. The existing dbt runner, reviewer,
  and dev service-account keys do not necessarily have these administrative permissions.
- You also need permission to manage Actions variables in the GitHub repository. The `gh`
  commands below require the GitHub CLI to be installed and authenticated; the GitHub web UI
  is an alternative.
- Check whether a suitable pool, provider, or service account already exists before creating
  it. The `create` commands below are for a new setup and will report an error if the same
  name already exists. Do not delete or replace an existing identity just to make them work.
- These commands create identities and change IAM permissions, but do not reset existing
  datasets or create service-account keys. The later manual test run uses BigQuery and can
  incur usage charges; see [BigQuery pricing](https://cloud.google.com/bigquery/pricing).

## One-time Google Cloud setup

Run the following blocks in the **same shell session**, so the shell variables remain available.
Lines beginning with `#` are explanations; the shell ignores them.

### 1. Select the project and find its number

```bash
# The project ID is the human-readable name used by gcloud commands and BigQuery.
PROJECT_ID=dbt-dqm

# Google also gives each project a numeric ID. Federation resource names and IAM principal
# identifiers use that project number, not the project ID or your billing account number.
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')

# Confirm that a number was returned before continuing. If this is empty or the previous
# command failed, check that you are signed in and have permission to view the project.
echo "$PROJECT_NUMBER"
```

### 2. Create the identity pool and GitHub provider

The pool is the container for external identities. The provider tells Google where the identity
comes from and what conditions a GitHub token must satisfy. Neither command grants BigQuery access
by itself.

```bash
# Create the pool named "github". "global" is the required location for this pool.
gcloud iam workload-identity-pools create github \
  --project="$PROJECT_ID" --location=global --display-name="GitHub Actions"

# Trust GitHub's OIDC issuer, but only tokens for this repository's main branch.
# The attribute mapping copies identity, repository, and ref claims from the GitHub token;
# the condition restricts which of those tokens Google will accept. A pull request or a
# different repository cannot satisfy this condition merely by knowing the provider name.
gcloud iam workload-identity-pools providers create-oidc dbt-dqm \
  --project="$PROJECT_ID" --location=global --workload-identity-pool=github \
  --display-name="dbt-dqm repository" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
  --attribute-condition="assertion.repository=='a-adamovich/dbt-dqm' && assertion.ref=='refs/heads/main'"
```

The repository and branch names above are security restrictions. If you move or rename the
repository or change its release branch, update and review the condition before running the
workflow under the new name.

### 3. Create the CI service account and grant BigQuery roles

A service account is a Google Cloud identity for software. This dedicated account keeps CI's
permissions separate from your personal, runner, and reviewer identities. These project-level
roles allow the account to run BigQuery jobs and create datasets; the account owns datasets it
creates. They do **not** by themselves grant access to read data in every existing dataset.
The workflow also checks that this identity cannot read the shared QA dataset.

```bash
# Create the account that the GitHub job will impersonate. This creates an identity, not a key.
gcloud iam service-accounts create dbt-dqm-ci --project="$PROJECT_ID" \
  --display-name="dbt-dqm on-demand CI"

# Grant the account permission to submit BigQuery jobs and to create test datasets.
# These commands add IAM bindings; they do not replace the project's other bindings.
for role in roles/bigquery.jobUser roles/bigquery.user; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:dbt-dqm-ci@$PROJECT_ID.iam.gserviceaccount.com" --role="$role"
done
```

`roles/bigquery.user` also permits listing dataset and table metadata, even though it does not
by itself grant access to all existing dataset contents. Other IAM grants can change the account's
access; the workflow's QA-dataset check verifies the effective access before running the suite.

### 4. Allow the trusted GitHub identity to use the service account

The provider authenticates the GitHub identity, but authentication alone does not let it act as
the service account. This binding grants the pool identities matching the repository attribute
the `roles/iam.workloadIdentityUser` role **on this one service account**. The provider's branch
condition from step 2 still applies.

```bash
gcloud iam service-accounts add-iam-policy-binding \
  "dbt-dqm-ci@$PROJECT_ID.iam.gserviceaccount.com" --project="$PROJECT_ID" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/attribute.repository/a-adamovich/dbt-dqm"

# Print the provider's full resource name; copy this value for the GitHub variable below.
echo "projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/providers/dbt-dqm"
```

If a command reports a permission error, have a project administrator review it rather than
using a more powerful service-account key. If an API is disabled, the administrator may also
need to enable it for the project.

## Configure GitHub repository variables

These variables tell the workflow which project and identity to use. They are configuration,
not passwords or private keys. You can set them under **Settings → Secrets and variables →
Actions → Variables** in the repository, or use the GitHub CLI commands below. The examples
assume the same shell session as the Google Cloud steps, including `PROJECT_NUMBER`.

```bash
# BigQuery project in which the workflow creates and runs its test datasets.
gh variable set DBT_DQM_BIGQUERY_PROJECT --repo a-adamovich/dbt-dqm --body "$PROJECT_ID"

# Google service-account identity the workflow impersonates.
gh variable set GCP_CI_SERVICE_ACCOUNT --repo a-adamovich/dbt-dqm \
  --body "dbt-dqm-ci@$PROJECT_ID.iam.gserviceaccount.com"

# Provider that verifies GitHub's token. This contains the numeric project number printed above.
gh variable set GCP_WORKLOAD_IDENTITY_PROVIDER --repo a-adamovich/dbt-dqm \
  --body "projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/providers/dbt-dqm"

# Enable the job only after the Google Cloud setup and other variables are in place.
# This does not start a run or enable a schedule; it permits future manual runs.
gh variable set BIGQUERY_LIFECYCLE_ENABLED --repo a-adamovich/dbt-dqm --body true
```

If using the web UI, enter the corresponding values shown in the commands. For the provider,
use the complete `projects/<PROJECT_NUMBER>/locations/global/workloadIdentityPools/github/providers/dbt-dqm`
value printed in step 4, replacing `<PROJECT_NUMBER>` with the actual number. Never paste a
service-account key into these variables.

## Start and inspect a manual run

The workflow must already be present on `main`; a workflow that exists only on a pull-request
branch cannot satisfy the release gate. In GitHub, open **Actions → BigQuery lifecycle → Run
workflow**, select `main`, and start the run. Alternatively:

```bash
# Start the workflow on main. This runs both the dbt 1 and dbt 2 BigQuery checks.
gh workflow run bigquery-lifecycle.yml --repo a-adamovich/dbt-dqm --ref main

# List recent runs and copy the ID of the run just started.
gh run list --repo a-adamovich/dbt-dqm --workflow bigquery-lifecycle.yml --limit 5

# Replace <RUN_ID> with that ID to watch its jobs and see whether they pass.
gh run watch <RUN_ID> --repo a-adamovich/dbt-dqm
```

If GitHub shows the lifecycle jobs as skipped, confirm the enable variable is exactly `true`
and the selected branch is `main`. Authentication errors may indicate an incorrect provider
name, a repository/branch mismatch, missing IAM bindings, or an API that is not enabled. A
successful manual run checks both engine generations; see the uploaded artifacts for details.
The old `DBT_DQM_BIGQUERY_KEYFILE_JSON` secret is not used by this workflow.

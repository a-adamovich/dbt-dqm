# Scheduled BigQuery verification

`.github/workflows/bigquery-scheduled.yml` runs every Monday (and on demand) from `main`:

1. a check that the CI identity **cannot** read the shared QA dataset (`dbt_dqm_qa`);
2. the full demo lifecycle in its own dataset, `dbt_dqm_scheduled_ci`;
3. the credentialed acceptance suite in parallel (`pytest -n 6`), each test in a disposable,
   labelled `dqm_acceptance_*` dataset;
4. a sweep of leftover acceptance datasets older than six hours.

Results are uploaded as the `scheduled-bigquery-results` artifact. A failure opens (or comments on)
one issue labelled `scheduled-bigquery`.

It authenticates **without a stored key**: GitHub's OIDC token for this repository's `main` branch
is exchanged through Workload Identity Federation for a short-lived token of a dedicated service
account. The workflow is inert until `BIGQUERY_LIFECYCLE_ENABLED` is `true`.

## One-time setup (a project owner)

Run in Cloud Shell or with an authenticated `gcloud`, as someone allowed to manage IAM in the
project:

```bash
PROJECT_ID=dbt-dqm
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')

# Pool and provider: only tokens for a-adamovich/dbt-dqm on refs/heads/main are accepted.
gcloud iam workload-identity-pools create github \
  --project="$PROJECT_ID" --location=global --display-name="GitHub Actions"
gcloud iam workload-identity-pools providers create-oidc dbt-dqm \
  --project="$PROJECT_ID" --location=global --workload-identity-pool=github \
  --display-name="dbt-dqm repository" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
  --attribute-condition="assertion.repository=='a-adamovich/dbt-dqm' && assertion.ref=='refs/heads/main'"

# Dedicated CI identity: run jobs and create (and own) its own datasets only.
gcloud iam service-accounts create dbt-dqm-ci --project="$PROJECT_ID" \
  --display-name="dbt-dqm scheduled CI"
for role in roles/bigquery.jobUser roles/bigquery.user; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:dbt-dqm-ci@$PROJECT_ID.iam.gserviceaccount.com" --role="$role"
done

# Let the repository's federated identity act as the CI service account.
gcloud iam service-accounts add-iam-policy-binding \
  "dbt-dqm-ci@$PROJECT_ID.iam.gserviceaccount.com" --project="$PROJECT_ID" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/attribute.repository/a-adamovich/dbt-dqm"

echo "projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/providers/dbt-dqm"
```

Then set the repository variables (no secrets are needed):

```bash
gh variable set DBT_DQM_BIGQUERY_PROJECT --body dbt-dqm
gh variable set GCP_CI_SERVICE_ACCOUNT --body dbt-dqm-ci@dbt-dqm.iam.gserviceaccount.com
gh variable set GCP_WORKLOAD_IDENTITY_PROVIDER --body "projects/<PROJECT_NUMBER>/locations/global/workloadIdentityPools/github/providers/dbt-dqm"
gh variable set BIGQUERY_LIFECYCLE_ENABLED --body true
```

Verify with one manual run: `gh workflow run bigquery-scheduled.yml --ref main`, then
`gh run watch`. The old `DBT_DQM_BIGQUERY_KEYFILE_JSON` secret is no longer used and can be
deleted.

`roles/bigquery.user` lets the CI account run jobs, create datasets, and own (read and write) the
datasets it creates. It can still list other datasets and their tables, but can't read their data:
the first workflow step verifies that it can't query `dbt_dqm_qa`.

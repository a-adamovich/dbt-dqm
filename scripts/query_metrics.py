"""Optional BigQuery validation usage measurement."""

import os
import re


def job_metrics(project, validation_id, started, finished, *, permissions=False):
    """Measure after the suite; exclude SCRIPT parents and separate unlabelled queries."""
    location = os.environ.get("DBT_DQM_BIGQUERY_TEST_LOCATION", "US").lower()
    if not project or not re.fullmatch(r"[a-z][a-z0-9-]+", project) or not re.fullmatch(r"[a-z0-9-]+", location):
        return {"status": "unavailable", "reason": "No valid measurement project/location"}
    try:
        from google.cloud import bigquery

        sql = f"""
        with window_jobs as (
          select job_id,parent_job_id,statement_type,labels,referenced_tables,
                 total_bytes_billed,total_slot_ms
          from `{project}.region-{location}.INFORMATION_SCHEMA.JOBS_BY_PROJECT`
          where creation_time between @started and @finished
        ), tagged as (
          select job_id from window_jobs
          where exists(select 1 from unnest(labels) l
                       where l.key='dbt_dqm_validation' and l.value=@validation_id)
        ), datasets as (
          select distinct r.dataset_id from window_jobs j,unnest(j.referenced_tables) r
          where j.job_id in (select job_id from tagged) or j.parent_job_id in (select job_id from tagged)
        )
        select if(job_id in (select job_id from tagged) or parent_job_id in (select job_id from tagged),
                  'labelled','unlabelled_app_or_other_activity') as category,
               count(*) as jobs, sum(coalesce(total_bytes_billed,0)) as billed_bytes,
               sum(coalesce(total_slot_ms,0)) as slot_ms
        from window_jobs
        where statement_type is distinct from 'SCRIPT'
          and (job_id in (select job_id from tagged) or parent_job_id in (select job_id from tagged)
               or exists(select 1 from unnest(referenced_tables) r
                         where r.dataset_id in (select dataset_id from datasets)
                            or (@permissions and r.dataset_id='dbt_dqm_qa')))
        group by category
        """
        config = bigquery.QueryJobConfig(maximum_bytes_billed=1024 ** 3, query_parameters=[
            bigquery.ScalarQueryParameter("started", "TIMESTAMP", started),
            bigquery.ScalarQueryParameter("finished", "TIMESTAMP", finished),
            bigquery.ScalarQueryParameter("validation_id", "STRING", validation_id),
            bigquery.ScalarQueryParameter("permissions", "BOOL", permissions)])
        rows = bigquery.Client(project=project, location=location).query(sql, job_config=config).result()
        return {"status": "measured", "categories": [dict(row.items()) for row in rows],
                "attribution": "Labels/parent labels and referenced datasets in the suite window; "
                               "unlabelled shared-QA activity may include other users."}
    except Exception as error:  # noqa: BLE001 - optional metrics must report unavailable, never zero
        # Missing existing metadata permissions must not silently appear as zero cost.
        return {"status": "unavailable", "reason": type(error).__name__}



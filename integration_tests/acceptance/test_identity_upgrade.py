"""Credentialed, serial cross-engine reproduction and repair on a disposable installation."""

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import tarfile

import pytest
from google.api_core.exceptions import BadRequest
from google.cloud import bigquery
from test_bigquery import ROOT, dedicated
from test_bigquery import demo as bigquery_demo_fixture

demo = bigquery_demo_fixture  # export the labelled, disposable-dataset fixture to pytest

CORE = os.environ.get('DBT_DQM_UPGRADE_CORE_EXECUTABLE')
V2 = os.environ.get('DBT_DQM_UPGRADE_V2_EXECUTABLE')
pytestmark = pytest.mark.skipif(not (CORE and V2 and os.environ.get('DBT_DQM_BIGQUERY_TEST_PROJECT')),
    reason='Cross-engine warehouse proof requires both isolated engines and disposable BigQuery access')


def test_core_v2_historical_incident_migration_repair_and_engine_roundtrip(demo, monkeypatch, tmp_path):
    historical_sha = 'b51a301168d5a0cf5b03b897fc4eb11a199e8e99'
    archive = subprocess.run(['git', 'archive', historical_sha], cwd=ROOT,
                             capture_output=True, check=True).stdout
    historical = tmp_path / 'historical-024'
    historical.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        bundle.extractall(historical, filter='data')
    package = demo.path / 'dbt_packages/dbt_dqm'
    package.unlink()
    package.symlink_to(historical, target_is_directory=True)
    monkeypatch.setenv('DBT_DQM_DBT_EXECUTABLE', CORE)
    demo.capture('initial')
    demo.dbt('build', '--select', 'package:dbt_dqm')
    demo.sql("update @dataset.dqm_issue_occurrences` set workflow_status='IN_PROGRESS', "
             "review_verdict='TRUE_POSITIVE', notes='preserve original review', "
             "ticket_url='https://example.invalid/issue',annotation_version=3 where record_status='Active'")
    before = demo.snapshot()
    original_ids = {row['occurrence_id'] for row in before}
    original_keys = {(row['test_unique_id'], row['unique_id']) for row in before}
    assert original_ids
    monkeypatch.setenv('DBT_DQM_DBT_EXECUTABLE', V2)
    demo.capture('recurrence')  # continuing, genuinely absent and genuinely new identities
    damaged_run = demo.dbt('run', '--select', 'dqm_reconcile')
    damaged = demo.snapshot()
    assert {row['occurrence_id'] for row in damaged if row['close_reason'] == 'IDENTITY_CHANGED'} == original_ids
    genuine_new_ids = {row['occurrence_id'] for row in damaged
        if (row['test_unique_id'], row['unique_id']) not in original_keys}
    package.unlink()
    package.symlink_to(ROOT, target_is_directory=True)
    demo.dbt('run-operation', 'ensure_capture_tables')
    # The real previous package must refuse the upgraded installation before capture changes.
    package.unlink()
    package.symlink_to(historical, target_is_directory=True)
    demo.dbt('run-operation', 'ensure_capture_tables', success=False)
    assert 'newer than this package' in demo.last_output
    package.unlink()
    package.symlink_to(ROOT, target_is_directory=True)

    spec = importlib.util.spec_from_file_location('repair105', ROOT / 'scripts/repair_identity_105.py')
    repair = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(repair)
    def table(name):
        return f'`{demo.client.project}.{demo.dataset}.{name}`'
    manifest = tmp_path / 'private-repair-manifest.json'
    arguments = ['--project-dir', str(demo.path), '--profiles-dir', str(demo.profiles),
                 '--run-id', damaged_run, '--output', str(manifest), '--dbt-executable', V2]
    paused = demo.snapshot()
    repair.main(arguments)
    assert demo.snapshot() == paused  # preview has no warehouse side effects
    private = manifest.read_bytes()
    bundle = json.loads(private)
    snapshot, plan = bundle['snapshot'], bundle['plan']
    digest = hashlib.sha256(private).hexdigest()
    parameters = [bigquery.ScalarQueryParameter(name, 'STRING', value) for name, value in (
        ('run_id', damaged_run), ('digest', digest),
        ('snapshot', repair.encoded(snapshot).decode()), ('plan', repair.encoded(plan).decode()))]
    query_config = bigquery.QueryJobConfig(maximum_bytes_billed=1024 ** 3, query_parameters=parameters)
    repair.main([*arguments, '--apply', digest])
    restored = demo.snapshot()
    repair.main([*arguments, '--apply', digest])
    assert demo.snapshot() == restored
    expected_ids = original_ids | genuine_new_ids
    assert {row['occurrence_id'] for row in restored} == expected_ids
    originals = {row['occurrence_id']: row for row in before}
    for row in restored:
        if row['occurrence_id'] in genuine_new_ids:
            assert row['workflow_status'] == 'NEW' and row['annotation_version'] == 0
            continue
        assert (row['record_status'], row['close_reason']) in [('Active', None), ('Archived', 'Passed')]
        for field in ('first_seen_at', 'first_seen_invocation', 'occurrence_number', 'notes',
                      'workflow_status', 'review_verdict', 'ticket_url', 'annotation_version'):
            assert row[field] == originals[row['occurrence_id']][field]
    assert not demo.sql("select 1 from @dataset.dqm_issue_events` where event_type='CLOSED_STRUCTURAL'")
    assert demo.sql("select count(*) n from @dataset.dqm_issue_events` where event_type='IDENTITY_REPAIRED'")[0]['n'] == len(original_ids)
    # The exact transaction is fenced after completion; no partial second apply can commit.
    with pytest.raises(BadRequest, match='Repair generation changed'):
        demo.client.query(repair.apply_sql(table), job_config=query_config).result()
    assert demo.snapshot() == restored
    for executable in (CORE, V2, CORE):
        monkeypatch.setenv('DBT_DQM_DBT_EXECUTABLE', executable)
        demo.dbt('run', '--select', 'demo_records', variables={'demo_scenario': 'recurrence'})
        dedicated(demo, variables={'demo_scenario': 'recurrence'})
        assert {row['occurrence_id'] for row in demo.snapshot()} == expected_ids
        assert not demo.sql("select 1 from @dataset.dqm_issue_occurrences` where close_reason='IDENTITY_CHANGED'")
    demo.capture('passed')
    demo.dbt('run', '--select', 'dqm_reconcile')
    demo.capture('recurrence')
    demo.dbt('run', '--select', 'dqm_reconcile')
    assert demo.sql("select 1 from @dataset.dqm_issue_events` where event_type='REAPPEARED'")
    print(json.dumps({'historical_sha': historical_sha, 'repaired_occurrences': len(original_ids)}))

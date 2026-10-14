"""Read-only exports and the real warehouse Apply path, including custom physical names."""
import csv
import json
import tempfile
from pathlib import Path

from dbt_dqm_app.browse import Filters
from dbt_dqm_app.exports import prepare_export
from dbt_dqm_app.store import Workspace
from dbt_dqm_app.warehouse import apply_patches, browse_population, browse_summary, fetch_issues


def check_review_actions(config):
    with tempfile.TemporaryDirectory(prefix='dqm-review-acceptance-') as directory:
        root=Path(directory); workspace=Workspace(root/'workspace.sqlite')
        rows=fetch_issues(config); assert rows
        workspace.replace_snapshot(rows)
        baseline=workspace.saved_rows_for([rows[0]['occurrence_id'],rows[-1]['occurrence_id']])
        assert len(baseline)==2
        workspace.set_change(baseline[0]['occurrence_id'],'notes','pending only, never export')
        assert workspace.stage_bulk(baseline,{'dqm_owner':'bulk reviewer','workflow_status':'TRIAGED',
                                              'review_verdict':'TRUE_POSITIVE'})==2
        for mode in ['snapshot','warehouse']:
            population=browse_population(config) if mode=='warehouse' else None
            expected=(browse_summary(config,population,Filters(lifecycle=()))['generation']
                      if population else workspace.last_sync())
            for format in ['csv','xlsx']:
                exported=prepare_export(config,workspace.path,Filters(lifecycle=()),root/(mode+format),
                                        format,population,expected)
                assert exported.rows==len(rows)
                if format=='csv':
                    with open(exported.path,encoding='utf-8-sig',newline='') as file:
                        records=list(csv.DictReader(file))
                    assert all(record['notes']!='pending only, never export' for record in records)
                    assert all(record['dqm_owner']!='bulk reviewer' for record in records)
                    assert all(isinstance(json.loads(record['record_values_json']),dict) for record in records)
        patches=workspace.pending(); assert apply_patches(config,patches)
        assert apply_patches(config,patches)  # same frozen request is idempotent
        refreshed={r['occurrence_id']:r for r in fetch_issues(config)}
        for row in baseline:
            saved=refreshed[row['occurrence_id']]
            assert saved['dqm_owner']=='bulk reviewer' and saved['workflow_status']=='TRIAGED'
            assert saved['review_verdict']=='TRUE_POSITIVE'
        assert refreshed[baseline[0]['occurrence_id']]['notes']=='pending only, never export'

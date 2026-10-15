import json
from types import SimpleNamespace

import pytest

from dbt_dqm_app.errors import AppError
from dbt_dqm_app.refresh import confirm_refresh
from dbt_dqm_app.selection import Selection
from dbt_dqm_app.store import Workspace


def issue(identity, version=0, installation='installed'):
    return {'occurrence_id': identity, 'annotation_version': version,
            '_installation_id': installation, 'dqm_owner': 'old', 'workflow_status': 'NEW',
            'review_verdict': 'UNREVIEWED', 'notes': None, 'record_values_json': '{"id":"001"}'}


def test_bulk_stages_evicted_baselines_preserving_other_pending_edits(tmp_path):
    workspace = Workspace(tmp_path / 'workspace.sqlite')
    first, second = issue('first'), issue('second')
    workspace.replace_snapshot([first, second])
    selection = Selection(); selection.add(workspace.saved_rows_for(['first', 'second']))
    workspace.set_change('first', 'notes', 'keep my notes')
    workspace.set_change('first', 'dqm_owner', 'previous draft')
    with workspace.connect() as connection:
        connection.execute("delete from snapshot where occurrence_id='second'")
    assert workspace.stage_bulk(selection.baselines(), {'dqm_owner': None, 'workflow_status': 'TRIAGED'}) == 2
    patches = {(p.occurrence_id, p.field_name): p for p in workspace.pending()}
    assert patches['first', 'notes'].new_value == 'keep my notes'
    assert patches['first', 'dqm_owner'].old_value == 'old'
    assert patches['first', 'dqm_owner'].new_value is None
    assert patches['first', 'dqm_owner'].version == 2
    assert patches['second', 'workflow_status'].base_annotation_version == 0
    assert patches['second', 'workflow_status'].installation_id == 'installed'


@pytest.mark.parametrize('failure', ['version', 'installation', 'pending'])
def test_bulk_failure_rolls_back_all_staging(tmp_path, failure):
    workspace = Workspace(tmp_path / 'workspace.sqlite')
    rows = [issue('a'), issue('b')]
    workspace.replace_snapshot(rows)
    workspace.set_change('a', 'notes', 'keep')
    if failure == 'version':
        with workspace.connect() as c:
            c.execute("update snapshot set payload_json=? where occurrence_id='b'", (json.dumps(issue('b', 1)),))
    elif failure == 'installation':
        rows[1]['_installation_id'] = 'old installation'
    else:
        workspace.set_change('b', 'notes', 'keep too')
        with workspace.connect() as c:
            c.execute("update pending_changes set base_annotation_version=2 where occurrence_id='b'")
    before = workspace.pending()
    with pytest.raises(AppError):
        workspace.stage_bulk(rows, {'workflow_status': 'RESOLVED'})
    assert workspace.pending() == before


def test_bulk_noops_and_explicit_return_to_saved_value(tmp_path):
    workspace = Workspace(tmp_path / 'workspace.sqlite'); row = issue('a')
    workspace.replace_snapshot([row]); workspace.set_change('a', 'dqm_owner', 'draft')
    assert workspace.stage_bulk([row], {'dqm_owner': 'old'}) == 1
    assert workspace.pending() == []
    assert workspace.stage_bulk([row], {'workflow_status': 'NEW'}) == 0


def test_selection_is_independent_bounded_and_never_changes_its_saved_base(monkeypatch):
    import dbt_dqm_app.selection as module
    selection = Selection(); row = issue('a'); selection.add([row]); row['dqm_owner'] = 'mutated'
    selection.add([issue('a', 2)])
    assert selection.baselines()[0]['annotation_version'] == 0
    assert selection.baselines()[0]['dqm_owner'] == 'old'
    monkeypatch.setattr(module, 'MAX_SELECTED_ISSUES', 1)
    with pytest.raises(AppError): selection.add([issue('b')])
    assert set(selection.rows) == {'a'}
    monkeypatch.setattr(module, 'MAX_SELECTED_BYTES', 1)
    with pytest.raises(AppError): selection.add([issue('b')])
    assert set(selection.rows) == {'a'}


def test_refresh_uses_resolved_names_and_noninteractive_requires_yes(monkeypatch, capsys):
    import sys
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False)
    workflow = SimpleNamespace(request={}, settings={'refresh': {'dqm_install': {
        'database': 'project', 'schema': 'custom_dev', 'identifier': 'alice_dqm_install'}}})
    manifest = SimpleNamespace(nodes={})
    config = SimpleNamespace(target='dev', project_id='project', dataset='custom_dev')
    with pytest.raises(ValueError, match='requires --yes'):
        confirm_refresh(config, manifest, workflow)
    assert 'project.custom_dev.alice_dqm_install' in capsys.readouterr().out
    workflow.request['yes'] = True
    assert confirm_refresh(config, manifest, workflow)


@pytest.mark.parametrize('answer,approved', [('',False),('no',False),('YES',True),('y',True),(None,False)])
def test_refresh_interactive_defaults_no_and_eof_cancels(monkeypatch, answer, approved):
    import sys
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    def respond(_):
        if answer is None: raise EOFError
        return answer
    monkeypatch.setattr('builtins.input', respond)
    config = SimpleNamespace(target='dev', project_id='project', dataset='dataset')
    assert confirm_refresh(config, SimpleNamespace(nodes={}),
                           SimpleNamespace(request={}, settings={'refresh': {'dqm_install': {
                               'database': 'p', 'schema': 's', 'identifier': 'dqm_install'}}})) is approved


def test_bulk_does_not_reversion_an_identical_pending_value(tmp_path):
    workspace=Workspace(tmp_path/'workspace.sqlite'); row=issue('a')
    workspace.replace_snapshot([row]); workspace.set_change('a','dqm_owner','draft')
    before=workspace.pending()
    assert workspace.stage_bulk([row],{'dqm_owner':'draft'})==0
    assert workspace.pending()==before

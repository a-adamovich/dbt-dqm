import csv
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile

import pytest

from dbt_dqm_app.browse import Filters
from dbt_dqm_app.errors import AppError
from dbt_dqm_app.exports import snapshot_rows, write_export
from dbt_dqm_app.store import Workspace


def issue(n=0, test='test.one', **changes):
    return {'occurrence_id': str(n), 'test_unique_id': test, 'test_name': "long/invalid:test name" * 3,
            'record_status':'Active','workflow_status':'NEW','dqm_owner':'saved', 'notes': 'line one\nУкраїна, second line',
            'first_seen_at':'2026-10-14T12:00:00+00:00', '_installation_id':'installed',
            'record_values_json':json.dumps({'id':'001', 'large_id':'12345678901234567890',
                                             'notes':'record notes', 'nested':{'label':'nested'}, 'array':[1,2]}),
            **changes}


def workbook_rows(path):
    ns = {'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with ZipFile(path) as archive:
        book = ET.fromstring(archive.read('xl/workbook.xml'))
        names = [s.attrib['name'] for s in book.findall('s:sheets/s:sheet', ns)]
        sheets=[]
        for i in range(1,len(names)+1):
            sheet=ET.fromstring(archive.read(f'xl/worksheets/sheet{i}.xml'))
            assert sheet.find('s:sheetViews/s:sheetView/s:pane',ns) is not None
            assert sheet.find('s:autoFilter',ns) is not None
            assert not sheet.findall('.//s:f', ns)
            rows=[]
            for row in sheet.findall('s:sheetData/s:row',ns):
                rows.append([''.join(cell.itertext()) for cell in row.findall('s:c',ns)])
            sheets.append(rows)
        return names,sheets


def test_csv_preserves_json_unicode_multiline_and_saved_values(tmp_path):
    workspace=Workspace(tmp_path/'workspace.sqlite'); records=[issue(i) for i in range(60)]
    workspace.replace_snapshot(records); workspace.set_change('0','dqm_owner','pending')
    result=write_export(snapshot_rows(workspace.path,Filters(owners=('saved',)),workspace.last_sync()),tmp_path/'out','csv')
    assert result.rows==60 and Path(result.path).read_bytes().startswith(b'\xef\xbb\xbf')
    with open(result.path,encoding='utf-8-sig',newline='') as f: rows=list(csv.DictReader(f))
    assert len(rows)==60 and rows[0]['dqm_owner']=='saved'
    assert rows[0]['notes']=='line one\nУкраїна, second line'
    assert json.loads(rows[0]['record_values_json'])==json.loads(records[0]['record_values_json'])
    assert '_installation_id' not in rows[0]
    assert workspace.pending()[0].new_value=='pending'


def test_excel_groups_tests_unions_keys_and_only_flattens_top_level(tmp_path):
    result=write_export(iter([issue(0,notes='=HYPERLINK("evil")'),
        issue(1,record_values_json='{"extra":"second","id":"002"}'),issue(2,'test.two')]),tmp_path,'xlsx')
    names,sheets=workbook_rows(result.path)
    assert len(names)==2 and len({n.casefold() for n in names})==2
    assert all(len(n)<=31 and '/' not in n and ':' not in n for n in names)
    head, row, second=sheets[0]; values=dict(zip(head,row))
    assert 'record_values_json' not in head and 'record.nested.label' not in head
    assert values['record.id']=='001' and values['record.large_id']=='12345678901234567890'
    assert json.loads(values['record.nested']) == {'label': 'nested'}
    assert json.loads(values['record.array']) == [1, 2]
    assert values['notes']=='=HYPERLINK("evil")' and values['record.notes']=='record notes'
    assert dict(zip(head,second))['record.extra']=='second'


@pytest.mark.parametrize('format',['csv','xlsx'])
def test_empty_export_has_headers(tmp_path,format):
    result=write_export(iter([]),tmp_path,format); assert result.rows==0
    if format=='xlsx':
        names,sheets=workbook_rows(result.path); assert names==['No issues'] and len(sheets[0])==1
    else:
        with open(result.path,encoding='utf-8-sig') as f: assert 'occurrence_id' in next(csv.reader(f))


@pytest.mark.parametrize('limit',['MAX_EXPORT_ISSUES','MAX_EXPORT_BYTES'])
def test_export_limit_never_offers_a_partial_file(tmp_path,monkeypatch,limit):
    import dbt_dqm_app.exports as module
    monkeypatch.setattr(module,limit,1)
    with pytest.raises(AppError): write_export(iter([issue(0),issue(1)]),tmp_path,'csv')
    assert not list(tmp_path.iterdir())


def test_excel_long_cells_fail_with_csv_fallback_without_truncation(tmp_path):
    with pytest.raises(AppError,match='CSV'): write_export(iter([issue(notes='x'*32768)]),tmp_path,'xlsx')
    assert not list(tmp_path.iterdir())


def test_csv_protects_formula_like_review_text(tmp_path):
    result=write_export(iter([issue(notes='  =SUM(1,2)')]),tmp_path,'csv')
    with open(result.path,encoding='utf-8-sig') as f: row=next(csv.DictReader(f))
    assert row['notes']=="'  =SUM(1,2)"


def test_snapshot_export_rejects_stale_sync_and_filters_archived(tmp_path):
    w=Workspace(tmp_path/'workspace.sqlite'); w.replace_snapshot([issue(0),issue(1,record_status='Archived')])
    with pytest.raises(AppError,match='changed'): list(snapshot_rows(w.path,Filters(),'old sync'))
    assert len(list(snapshot_rows(w.path,Filters(),w.last_sync())))==1
    assert len(list(snapshot_rows(w.path,Filters(lifecycle=('Archived',)),w.last_sync())))==1


@pytest.mark.parametrize('payload', ['{"bad":"\\u0001"}', json.dumps({str(n): n for n in range(16_384)})], ids=['control-character', 'too-many-columns'])
def test_excel_incompatible_values_or_columns_fail_with_csv_fallback(tmp_path, payload):
    with pytest.raises(AppError, match='CSV'):
        write_export(iter([issue(record_values_json=payload)]), tmp_path, 'xlsx')
    assert not list(tmp_path.iterdir())


def test_failed_stream_closes_source_and_removes_partial_export(tmp_path):
    closed = []

    def rows():
        try:
            yield issue()
            raise AppError('Warehouse generation changed')
        finally:
            closed.append(True)

    with pytest.raises(AppError, match='generation changed'):
        write_export(rows(), tmp_path, 'csv')
    assert closed == [True] and not list(tmp_path.iterdir())


def test_completed_file_limit_is_independent_of_source_limit(tmp_path, monkeypatch):
    import dbt_dqm_app.exports as module

    def refuse(path):
        assert path.exists()
        raise AppError('The completed export exceeds 128 MiB')

    monkeypatch.setattr(module, '_check_file', refuse)
    with pytest.raises(AppError, match='completed export'):
        write_export(iter([issue()]), tmp_path, 'xlsx')
    assert not list(tmp_path.iterdir())


def test_excel_line_feed_limit_preserves_complete_csv_fallback(tmp_path):
    notes = "line\n" * 254
    with pytest.raises(AppError, match='CSV'):
        write_export(iter([issue(notes=notes)]), tmp_path, 'xlsx')
    assert not list(tmp_path.iterdir())
    result = write_export(iter([issue(notes=notes)]), tmp_path, 'csv')
    with open(result.path, encoding='utf-8-sig', newline='') as output:
        assert next(csv.DictReader(output))['notes'] == notes


def test_excel_preserves_numeric_tokens_and_complete_nested_json(tmp_path):
    raw = '{"amount":0.12345678901234567890123456789,"huge":123456789012345678901234567890,"nested":{"amount":0.12345678901234567890123456789,"duplicate":1,"duplicate":2}}'
    result = write_export(iter([issue(record_values_json=raw)]), tmp_path, 'xlsx')
    _, sheets = workbook_rows(result.path)
    values = dict(zip(*sheets[0]))
    assert values['record.amount'] == '0.12345678901234567890123456789'
    assert values['record.huge'] == '123456789012345678901234567890'
    assert values['record.nested'] == '{"amount":0.12345678901234567890123456789,"duplicate":1,"duplicate":2}'


@pytest.mark.parametrize('raw', ['{"a":1,}', '{"a":1,"a":2}', '{} trailing', '{"a" 1}', '{', '{1:2}', '{"a":NaN}'])
def test_excel_refuses_unexpandable_record_json_without_partial_files(tmp_path, raw):
    with pytest.raises(AppError, match='CSV'):
        write_export(iter([issue(record_values_json=raw)]), tmp_path, 'xlsx')
    assert not list(tmp_path.iterdir())


def test_review_order_granularity_and_first_seen_record_fields(tmp_path):
    rows=[issue(record_values_json='{"z":"z","id":"001","a":"a"}'),
          issue(1,record_values_json='{"b":"b","id":"002","z":"z"}')]
    output=write_export(iter(rows),tmp_path/'xlsx','xlsx',{'test.one':['a','ID']})
    _,sheets=workbook_rows(output.path)
    header=sheets[0][0]
    assert header[:6]==['test_name','dqm_owner','record.a','record.id','record.z','record.b']
    assert header[6:10]==['record_status','workflow_status','review_verdict','test_status']
    assert header[10:16]==['first_seen_at (UTC)','last_seen_at (UTC)','annotation_updated_at (UTC)','call_to_action','ticket_url','notes']
    output=write_export(iter(rows),tmp_path/'csv','csv')
    with open(output.path,encoding='utf-8-sig') as file:
        header=next(csv.reader(file))
    assert header[:3]==['test_name','dqm_owner','record_values_json']
    assert header.index('notes') < header.index('occurrence_id')


def test_timezone_labels_preserve_precision_and_captured_json(tmp_path):
    timestamp='2026-10-14T12:13:23.175000123+00:00'
    output=write_export(iter([issue(first_seen_at=timestamp,last_seen_at='2026-10-14T11:00:00',
                                  annotation_updated_at='2026-10-14T08:00:00-04:00')]),tmp_path,'csv')
    with open(output.path,encoding='utf-8-sig') as file:row=next(csv.DictReader(file))
    assert row['first_seen_at (UTC)']==timestamp
    assert row['last_seen_at (timezone unspecified)']=='2026-10-14T11:00:00'
    assert row['annotation_updated_at (offset included)']=='2026-10-14T08:00:00-04:00'
    assert row['record_values_json']==issue()['record_values_json']


def test_csv_retains_json_that_excel_cannot_expand(tmp_path):
    raw='{"duplicate":1,"duplicate":2}'
    with pytest.raises(AppError,match='CSV'):
        write_export(iter([issue(record_values_json=raw)]),tmp_path/'xlsx','xlsx')
    output=write_export(iter([issue(record_values_json=raw)]),tmp_path/'csv','csv')
    with open(output.path,encoding='utf-8-sig') as file:
        assert next(csv.DictReader(file))['record_values_json']==raw

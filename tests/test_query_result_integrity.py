from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd
import pytest
from openpyxl import load_workbook

from backend.agent.contracts import TaskContract
from backend.agent.store import RunStore
from backend.services.datasets import frame_records, load_result_frame
from backend.services.query_results import normalize_result_columns, read_result_frame, write_result_frame
from backend.services.results.manifests import ResultService


def publish_result(app, client, source, result):
    response = client.post('/api/analyses', json={
        'objective': '核对结果明细', 'source_ids': [source['id']],
    })
    assert response.status_code == 201
    run = response.get_json()['item']
    store = RunStore(app.extensions['meridian_db'])
    store.add_contract(
        run['id'], TaskContract.from_payload(run['contract']['payload']),
        expected_version=1, confirmed_by='local-default',
    )
    run = store.get_run(run['id'])
    contract = store.latest_contract(run['id'])
    service = ResultService(store.db)
    validation = {
        'status': 'PASS', 'quality_score': 1, 'coverage': 1,
        'issues': [], 'items': [], 'scoring_note': 'fixture',
    }
    manifest = service.create_manifest(run, contract, '结果已核对。', [result['id']], validation, [])
    service.publish(run, contract, manifest, validation)
    return run, manifest


def test_real_details_and_exports_preserve_values(app, client, source):
    sql = """SELECT CASE WHEN sales=120 THEN '00123' ELSE 'NA' END AS code,
        CASE WHEN sales=90 THEN '' ELSE 'NULL' END AS label,
        CASE WHEN sales=90 THEN NULL ELSE sales / 1000000.0 END AS amount,
        sales > 100 AS enabled,
        CASE WHEN sales=105 THEN 1.0/0 ELSE cost END AS unstable FROM sales ORDER BY sales"""
    response = client.post('/api/query', json={'source_ids': [source['id']], 'sql': sql})
    assert response.status_code == 200, response.get_json()
    result = response.get_json()['result']
    expected = result['data']
    assert expected[1]['unstable'] is None
    json.loads(response.get_data(as_text=True), parse_constant=lambda value: pytest.fail(value))
    database = app.extensions['meridian_db']
    persisted = database.get('query_results', result['id'], workspace_id='default')
    assert Path(persisted['path']).suffix == '.parquet'
    with app.app_context():
        assert frame_records(load_result_frame(result['id'])) == expected
    run, manifest = publish_result(app, client, source, result)
    details = client.get(f"/api/analyses/{run['id']}/details?cursor=0&limit=2")
    assert details.status_code == 200, details.get_data(as_text=True)
    # Use a strict parser: Flask's permissive get_json accepts invalid NaN.
    data = json.loads(details.get_data(as_text=True), parse_constant=lambda value: pytest.fail(value))
    assert data['items'] == expected[:2]
    assert data['next_cursor'] == 2
    page = client.get(f"/api/analyses/{run['id']}/details?cursor=2&limit=500").get_json()
    assert page['items'] == expected[2:]
    assert page['next_cursor'] is None
    assert '00123' in [row['code'] for row in page['items']]
    assert manifest['payload']['charts'][0]['option']['xAxis']['data'] == [row['code'] for row in expected]
    exported = client.post(f"/api/analyses/{run['id']}/artifacts", json={'kinds': ['data_xlsx']})
    assert exported.status_code == 201, exported.get_json()
    artifact = database.get('artifacts', exported.get_json()['items'][0]['id'], workspace_id='default')
    workbook = load_workbook(artifact['path'], read_only=True)
    values = list(workbook['验证数据'].values)
    assert [row[0] for row in values[1:]] == [row['code'] for row in expected]
    assert values[1][2] is None
    assert values[1][3] is False
    workbook.close()


@pytest.mark.parametrize('format_name', ['csv', 'parquet'])
def test_pages_advance_by_records_and_preserve_late_string_codes(app, format_name):
    rows = [{'code': f'{index:05d}', 'note': f'第一行\n第二行 {index}', 'amount': float(index) if index % 7 else None}
            for index in range(620)]
    frame = pd.DataFrame(rows)
    path = app.config['SETTINGS'].export_dir / f'multiline.{format_name}'
    if format_name == 'csv':
        frame.to_csv(path, index=False)
    else:
        frame.to_parquet(path, index=False)
    result = {'path': str(path), 'rows': len(frame), 'columns': list(frame.columns), 'data': frame_records(frame, 300)}
    with app.app_context():
        actual = frame_records(read_result_frame(result, offset=500, limit=50), 50)
        expected = frame_records(frame.iloc[500:550], 50)
        assert actual == expected
        assert read_result_frame(result, offset=620, limit=50).empty


def test_empty_result_and_legacy_exact_preview(app):
    frame = pd.DataFrame({'code': ['00123', 'NA', '', None], 'value': [1.5, None, 0, -2.0]})
    path = app.config['SETTINGS'].export_dir / 'legacy.csv'
    frame.to_csv(path, index=False)
    result = {'path': str(path), 'columns': list(frame.columns), 'rows': len(frame), 'data': frame_records(frame)}
    with app.app_context():
        assert frame_records(read_result_frame(result, limit=50)) == result['data']
        empty = pd.DataFrame({'code': pd.Series(dtype='string'), 'value': pd.Series(dtype='float64')})
        empty_path = path.with_suffix('.parquet')
        empty.to_parquet(empty_path, index=False)
        assert read_result_frame({'path': str(empty_path)}, limit=50).empty


def test_parquet_analysis_results_can_be_published_and_paginated(app, client, source):
    frame = pd.DataFrame({'segment': ['A', 'B'], 'amount': [0.0001, None]})
    path = app.config['SETTINGS'].export_dir / 'analysis.parquet'
    frame.to_parquet(path, index=False)
    result = app.extensions['meridian_db'].put('query_results', {
        'id': 'analysis-result', 'path': str(path), 'rows': 2, 'total_rows': 2,
        'source_ids': [source['id']], 'actor_id': 'local-default', 'columns': list(frame.columns),
        'data': frame_records(frame), 'completeness': 'complete', 'accuracy': 'exact',
    }, workspace_id='default')
    run, _ = publish_result(app, client, source, result)
    details = client.get(f"/api/analyses/{run['id']}/details").get_json()
    assert details['items'] == result['data']


def test_result_reader_rejects_paths_outside_managed_exports(app, tmp_path):
    path = tmp_path / 'private.csv'
    path.write_text('secret\n1\n', encoding='utf-8')
    with app.app_context(), pytest.raises(PermissionError):
        read_result_frame({'path': str(path), 'rows': 1})


def test_typed_storage_preserves_duplicate_and_mixed_database_columns(app):
    frame = pd.DataFrame([[1, '00123', 'NA', 1.5], ['A', '', None, None]], columns=['value', 'value', 'value_2', 'amount'])
    frame = normalize_result_columns(frame)
    assert list(frame.columns) == ['value', 'value_3', 'value_2', 'amount']
    path = write_result_frame(frame, app.config['SETTINGS'].export_dir / 'mixed.parquet')
    assert path.suffix == '.json'
    with app.app_context():
        assert frame_records(read_result_frame({'path': str(path)}, offset=1, limit=1)) == frame_records(frame.iloc[1:])
        assert frame_records(read_result_frame({'path': str(path)})) == frame_records(frame)
        assert list(read_result_frame({'path': str(path)}).select_dtypes('number').columns) == ['amount']


def test_real_sqlite_duplicate_aliases_and_mixed_values_retain_every_column(app, client):
    path = app.config['SETTINGS'].storage_dir / 'mixed.sqlite3'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE records (value, code TEXT)')
        connection.executemany('INSERT INTO records VALUES (?,?)', [(1, '00123'), ('A', 'NA')])
    registered = client.post('/api/sources/database', json={'name': 'mixed', 'url': f'sqlite:///{path}'})
    assert registered.status_code == 201, registered.get_json()
    source = registered.get_json()['item']
    response = client.post('/api/query', json={'source_ids': [source['id']],
                                             'sql': 'SELECT value AS value, code AS value, code AS value_2 FROM records ORDER BY code'})
    assert response.status_code == 200, response.get_json()
    result = response.get_json()['result']
    assert result['columns'] == ['value', 'value_3', 'value_2']
    assert result['data'] == [{'value': 1, 'value_3': '00123', 'value_2': '00123'},
                              {'value': 'A', 'value_3': 'NA', 'value_2': 'NA'}]
    with app.app_context():
        assert frame_records(load_result_frame(result['id'])) == result['data']


@pytest.mark.parametrize('format_name', ['parquet', 'csv'])
def test_nonfinite_legacy_results_become_missing_values_instead_of_invalid_json(app, format_name):
    frame = pd.DataFrame({'amount': [float('inf'), float('-inf'), float('nan'), 0.0, 0.001]})
    path = app.config['SETTINGS'].export_dir / f'nonfinite.{format_name}'
    if format_name == 'parquet':
        frame.to_parquet(path, index=False)
    else:
        frame.to_csv(path, index=False)
    result = {'path': str(path), 'columns': ['amount'], 'data': [{'amount': float('inf')}, {'amount': float('-inf')}]}
    with app.app_context():
        actual = read_result_frame(result, offset=2, limit=3)
        assert frame_records(actual) == [{'amount': None}, {'amount': 0.0}, {'amount': 0.001}]
        assert frame_records(read_result_frame(result)) == [
            {'amount': None}, {'amount': None}, {'amount': None}, {'amount': 0.0}, {'amount': 0.001},
        ]
        assert not read_result_frame(result)['amount'].isin([float('inf'), float('-inf')]).any()
        json.dumps(frame_records(read_result_frame(result)), allow_nan=False)

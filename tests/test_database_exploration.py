from __future__ import annotations

import sqlite3
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from backend.services.agent_tools import AgentToolContext, execute_tool
from backend.services.database_queries import estimate_query
from backend.services.sql_security import validate_read_only_sql


def register_fixture(app, client, *, tables=1, rows=800, width=2):
    path = app.config['SETTINGS'].storage_dir / 'exploration.sqlite3'
    with sqlite3.connect(path) as connection:
        for index in range(tables):
            fields = ','.join(f'field_{column:03d} TEXT' for column in range(width))
            connection.execute(f'CREATE TABLE table_{index:03d} ({fields})')
        connection.executemany('INSERT INTO table_000 VALUES (' + ','.join('?' for _ in range(width)) + ')',
            [tuple(str(index) if column == 0 else None if index % 2 else 'value' for column in range(width)) for index in range(rows)])
    response = client.post('/api/sources/database', json={'name': '本地隔离数据库', 'url': f'sqlite:///{path}'})
    assert response.status_code == 201, response.get_json()
    return response.get_json()['item'], path


def wait_job(client, job_id, *, terminal=True):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f'/api/jobs/{job_id}')
        assert response.status_code == 200, response.get_json()
        job = response.get_json()['item']
        if job['status'] in ({'completed', 'failed', 'cancelled', 'blocked'} if terminal else {'running'}):
            return job
        time.sleep(0.02)
    pytest.fail('Database job did not reach the expected state')


def test_all_catalog_pages_and_late_table_columns_survive_refresh(app, client):
    source, path = register_fixture(app, client, tables=215, rows=0)
    assert source['catalog_total'] == 215
    assert source['catalog_has_more'] is True
    assert len(source['tables']) == 100
    cursor, names = '', []
    while True:
        response = client.get(f"/api/sources/{source['id']}/catalog?limit=17&cursor={cursor}")
        assert response.status_code == 200
        page = response.get_json()
        names.extend(table['name'] for table in page['items'])
        cursor = page['next_cursor']
        if cursor is None:
            break
    assert names == [f'table_{index:03d}' for index in range(215)]
    with sqlite3.connect(path) as connection:
        connection.execute('ALTER TABLE table_214 ADD COLUMN new_field INTEGER')
    refreshed = client.post(f"/api/sources/{source['id']}/refresh")
    assert refreshed.status_code == 200
    columns = client.get(f"/api/sources/{source['id']}/schema?table=table_214").get_json()['schema']['tables'][0]['columns']
    assert [column['name'] for column in columns] == ['field_000', 'field_001', 'new_field']
    queried = client.post('/api/query', json={'source_ids': [source['id']], 'sql': 'SELECT new_field FROM table_214'})
    assert queried.status_code == 200, queried.get_json()


def test_catalog_scope_search_and_wide_schema_are_lossless(app, client):
    source, _ = register_fixture(app, client, tables=3, width=273, rows=0)
    patched = client.patch(f"/api/sources/{source['id']}", json={'analysis_tables': ['table_002']})
    assert patched.status_code == 200
    directory = client.get(f"/api/sources/{source['id']}/catalog").get_json()
    assert [table['name'] for table in directory['items']] == ['table_002']
    assert client.get(f"/api/sources/{source['id']}/schema?table=table_000").status_code == 403
    collected, offset = [], 0
    while True:
        response = client.get(f"/api/sources/{source['id']}/schema?table=table_002&column_offset={offset}&column_limit=40")
        table = response.get_json()['schema']['tables'][0]
        collected.extend(column['name'] for column in table['columns'])
        offset = table['next_column_offset']
        if offset is None:
            break
    assert collected == [f'field_{index:03d}' for index in range(273)]
    assert client.get(f"/api/sources/{source['id']}/catalog?q=missing").get_json()['total'] == 0
    with app.app_context():
        context = AgentToolContext(app.extensions['meridian_db'], 'default', 'review', [source['id']])
        result, _ = execute_tool('get_schema', {}, context)
        assert [table['name'] for table in result['sources'][0]['tables']] == ['table_002']
        result, _ = execute_tool('get_schema', {'tables': ['table_002'], 'column_offset': 250}, context)
        assert len(result['sources'][0]['tables'][0]['columns']) == 23


def test_sample_and_scope_aggregate_have_distinct_accuracy_and_cache(app, client):
    source, _ = register_fixture(app, client)
    sample = client.get(f"/api/sources/{source['id']}/profile?table=table_000&sample_rows=20").get_json()['profile']
    assert sample['rows'] == 20
    assert sample['completeness'] == 'partial'
    assert sample['accuracy'] == 'sample_based'
    assert '不能推断全表' in sample['sampling_note']
    aggregate = client.get(f"/api/sources/{source['id']}/profile?table=table_000&mode=aggregate&column=field_001")
    assert aggregate.status_code == 200, aggregate.get_json()
    value = aggregate.get_json()['profile']
    assert value['rows'] == 800 and value['columns'][0]['missing'] == 400
    assert value['columns'][0]['unique'] == 1
    assert value['accuracy'] == 'exact' and value['sampled'] is False
    assert value['duplicate_rows'] is None
    assert client.get(f"/api/sources/{source['id']}/profile?table=table_000&mode=aggregate&column=field_001").get_json()['profile']['cached']
    with app.app_context():
        context = AgentToolContext(app.extensions['meridian_db'], 'default', 'review', [source['id']])
        remote, _ = execute_tool('profile_data', {'source_id': source['id'], 'table': 'table_000', 'sample_rows': 10}, context)
        assert remote['completeness'] == 'partial'
        assert remote['profile']['sample_rows'] == 10
        partial, _ = execute_tool('profile_data', {'result_id': remote['result_id']}, context)
        assert partial['completeness'] == 'partial'


def test_stream_budget_rejects_before_persisting_large_result(app, client):
    source, path = register_fixture(app, client, rows=0)
    with sqlite3.connect(path) as connection:
        connection.executemany('INSERT INTO table_000 VALUES (?,?)', [('id', 'x' * 3000)] * 100)
    app.config['SETTINGS'] = replace(app.config['SETTINGS'], max_query_bytes=8000)
    response = client.post('/api/query', json={'source_ids': [source['id']], 'sql': 'SELECT * FROM table_000'})
    assert response.status_code == 400, response.get_json()
    assert '字节预算' in response.get_json()['error']
    assert not app.extensions['meridian_db'].list('query_results', workspace_id='default')


def test_async_queries_complete_and_cancel_expensive_read_only_work(app, client):
    source, _ = register_fixture(app, client, rows=10)
    submitted = client.post('/api/query/jobs', json={'source_ids': [source['id']], 'sql': 'SELECT COUNT(*) AS n FROM table_000'})
    assert submitted.status_code == 202, submitted.get_json()
    completed = wait_job(client, submitted.get_json()['job']['id'])
    assert completed['status'] == 'completed', completed
    assert completed['result']['result']['data'] == [{'n': 10}]
    slow = client.post('/api/query/jobs', json={'source_ids': [source['id']], 'sql':
        'WITH RECURSIVE numbers(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM numbers WHERE n<1000000000) SELECT SUM(n) AS total FROM numbers'})
    assert slow.status_code == 202
    job_id = slow.get_json()['job']['id']
    wait_job(client, job_id, terminal=False)
    assert client.post(f'/api/jobs/{job_id}/cancel').status_code == 200
    assert wait_job(client, job_id)['status'] == 'cancelled'
    after = client.post('/api/query', json={'source_ids': [source['id']], 'sql': 'SELECT COUNT(*) AS n FROM table_000'})
    assert after.status_code == 200 and after.get_json()['result']['data'] == [{'n': 10}]
    app.extensions['meridian_jobs'].shutdown()


def test_doris_cost_gate_rejects_large_scan_and_cross_join_without_execution():
    class Connection:
        engine = SimpleNamespace(dialect=SimpleNamespace(name='mysql'))
        def exec_driver_sql(self, statement, **_kwargs):
            assert str(statement).startswith('EXPLAIN ')
            return SimpleNamespace(all=lambda: [('0:VOlapScanNode\n cardinality=95000000\n partitions=1/1',)])
    with pytest.raises(ValueError, match='超过单次查询预算'):
        estimate_query(Connection(), 'SELECT COUNT(DISTINCT device) FROM devices', {'engine_family': 'doris'})
    with pytest.raises(ValueError, match='关联条件'):
        estimate_query(Connection(), 'SELECT * FROM devices CROSS JOIN orders', {'engine_family': 'doris'})


@pytest.mark.parametrize('sql', ["SELECT /*+ SET_VAR(query_timeout=9000) */ * FROM orders",
                               "SELECT * FROM S3('https://example.com/data')",
                               'SELECT * INTO copied_orders FROM orders', 'SELECT * FROM orders FOR UPDATE'])
def test_queries_cannot_override_limits_or_read_external_tables(sql):
    with pytest.raises(ValueError):
        validate_read_only_sql(sql, 'mysql')


def test_background_profile_matches_sync_and_refresh_invalidates_cache(app, client):
    source, path = register_fixture(app, client, rows=10)
    submitted = client.post(f"/api/sources/{source['id']}/profile-jobs", json={'table': 'table_000', 'mode': 'aggregate'})
    assert submitted.status_code == 202
    job = wait_job(client, submitted.get_json()['job']['id'])
    assert job['status'] == 'completed', job
    assert job['result']['profile']['rows'] == 10
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO table_000 VALUES ('new','new')")
    assert client.post(f"/api/sources/{source['id']}/refresh").status_code == 200
    refreshed = client.get(f"/api/sources/{source['id']}/profile?table=table_000&mode=aggregate").get_json()['profile']
    assert refreshed['rows'] == 11 and refreshed['cached'] is False
    app.extensions['meridian_jobs'].shutdown()


def test_profile_cache_respects_changed_row_and_column_policies(app, client):
    source, _ = register_fixture(app, client, rows=20)
    url = f"/api/sources/{source['id']}/profile?table=table_000&mode=aggregate&column=field_000"
    assert client.get(url).get_json()['profile']['rows'] == 20
    patched = client.patch(f"/api/sources/{source['id']}", json={
        'row_policy': {'table': 'table_000', 'column': 'field_000', 'allow': {'*': ['0','1']}},
        'column_policies': {'table': 'table_000', 'deny': {'*': ['field_001']}},
    })
    assert patched.status_code == 200, patched.get_json()
    value = client.get(url).get_json()['profile']
    assert value['rows'] == 2 and value['cached'] is False
    assert client.get(f"/api/sources/{source['id']}/profile?mode=sample&column=field_001").status_code == 403
    catalog = client.get(f"/api/sources/{source['id']}/catalog").get_json()['items'][0]
    assert catalog['rows'] is None and catalog['columns'] is None


def test_string_amount_requires_explicit_conversion_and_unit_scale(app, client):
    from decimal import Decimal
    from backend.services.semantic import execute_metric_query, save_metric, save_model

    source, path = register_fixture(app, client, rows=0)
    with sqlite3.connect(path) as connection:
        connection.executemany('INSERT INTO table_000 VALUES (?,?)', [('100','north'),('250','south')])
    database = app.extensions['meridian_db']
    definition = {'source_id': source['id'], 'table': 'table_000', 'name': '金额模型',
                  'measures': [{'name': 'amount', 'column': 'field_000', 'aggregation': 'sum'}]}
    with app.app_context():
        with pytest.raises(ValueError, match='数值转换'):
            save_model(database, definition, 'default', 'local-default')
        definition['measures'][0].update(numeric_cast='decimal', scale='0.01')
        model = save_model(database, definition, 'default', 'local-default')
        metric = save_metric(database, {'model_id': model['id'], 'name': 'amount_yuan', 'label': '金额',
                                        'measure': 'amount', 'status': 'approved', 'unit': '元'}, 'default', 'local-default')
        output = execute_metric_query(database, {'metric': metric['id']}, 'default', 'local-default')
        assert Decimal(str(output['result']['data'][0]['amount_yuan'])) == Decimal('3.5')
        assert 'CAST(' in output['plan']['sql'] and '0.01' in output['plan']['sql']
        for invalid in ['0', '-1', 'NaN', 'Infinity', '1;DROP TABLE orders']:
            definition['measures'][0]['scale'] = invalid
            with pytest.raises(ValueError):
                save_model(database, definition, 'default', 'local-default')


def test_query_rechecks_permissions_before_publishing_results(app, client, monkeypatch):
    from backend.services import database_queries

    source, _ = register_fixture(app, client, rows=10)
    original = database_queries.read_bounded
    def changed_policy(*args, **kwargs):
        frame, size = original(*args, **kwargs)
        app.extensions['meridian_db'].patch('sources', source['id'], {
            'row_policy': {'table':'table_000','column':'field_000','allow':{'*':['0']}}}, workspace_id='default')
        return frame, size
    monkeypatch.setattr(database_queries, 'read_bounded', changed_policy)
    response = client.post('/api/query',json={'source_ids':[source['id']],'sql':'SELECT * FROM table_000'})
    assert response.status_code == 403
    assert '权限在查询期间' in response.get_json()['error']
    assert not app.extensions['meridian_db'].list('query_results',workspace_id='default')


def test_compact_string_date_range_is_explicit_and_applied(app, client):
    source, path = register_fixture(app, client, rows=0)
    with sqlite3.connect(path) as connection:
        connection.executemany('INSERT INTO table_000 VALUES (?,?)',[('20261001','a'),('20261011','b'),('20261012','c')])
    response = client.get(f"/api/sources/{source['id']}/profile?mode=aggregate&table=table_000&column=field_001"
                          '&time_column=field_000&time_start=2026-10-01&time_end=2026-10-12&time_format=yyyyMMdd')
    assert response.status_code == 200, response.get_json()
    value = response.get_json()['profile']
    assert value['rows'] == 2 and value['scope']['time_format'] == 'yyyyMMdd'


def test_doris_dialect_keeps_supported_approximate_function_name():
    from backend.services.datasets import _dialect_name
    from backend.services.sql_security import bounded_read_only_sql

    engine = SimpleNamespace(dialect=SimpleNamespace(name='mysql'), _meridian_family='doris')
    bounded = bounded_read_only_sql('SELECT APPROX_COUNT_DISTINCT(device) FROM devices',2,_dialect_name(engine))
    assert 'APPROX_COUNT_DISTINCT(' in bounded


def test_native_query_literals_are_not_driver_bind_parameters(app, client):
    source, _ = register_fixture(app,client,rows=1)
    response=client.post('/api/query',json={'source_ids':[source['id']],
        'sql':"SELECT ':branch' AS label, '100% verified' AS note FROM table_000"})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()['result']['data'] == [{'label':':branch','note':'100% verified'}]


def test_legacy_database_registration_is_upgraded_on_first_read(app, client):
    source,_=register_fixture(app,client,rows=2)
    database=app.extensions['meridian_db']
    stored=database.get('sources',source['id'],workspace_id='default')
    stored.pop('engine_family')
    database.put('sources',stored,workspace_id='default')
    response=client.post('/api/query',json={'source_ids':[source['id']], 'sql':'SELECT COUNT(*) AS n FROM table_000'})
    assert response.status_code == 200 and response.get_json()['result']['data'] == [{'n':2}]
    assert database.get('sources',source['id'],workspace_id='default')['engine_family'] == 'sqlite'


def test_large_table_bounded_preview_and_pruned_scan_budgets():
    class Connection:
        engine = SimpleNamespace(dialect=SimpleNamespace(name='mysql'))
        def exec_driver_sql(self, statement, **_kwargs):
            return SimpleNamespace(all=lambda: [('0:VOlapScanNode\n TABLE: warehouse.devices\n'
                ' cardinality=1\n partitions=1/10\n tablets=1/10',)])
    source = {'engine_family':'doris','tables':[{'name':'devices','rows':900_000_000,'data_bytes':90_000_000_000}]}
    preview = estimate_query(Connection(), 'SELECT device FROM devices LIMIT 501', source)
    assert preview['status'] == 'bounded_preview' and preview['estimated_scan_rows'] == 501
    # Partition/tablet ratios must not be multiplied: both describe the same pruning.
    with pytest.raises(ValueError, match='90,000,000'):
        estimate_query(Connection(), 'SELECT COUNT(*) FROM devices WHERE day >= 20261001', source)


def test_mysql_wire_budget_rejects_packet_before_reading_or_draining():
    from backend.services.database_queries import read_bounded
    reads = []
    class Raw:
        def _read_bytes(self, size):
            reads.append(size)
            return b'x' * size
    raw = Raw()
    original = raw._read_bytes
    class Connection:
        engine = SimpleNamespace(dialect=SimpleNamespace(name='mysql'))
        connection = SimpleNamespace(driver_connection=raw)
        invalidated = False
        def execution_options(self, **_kwargs):
            return self
        def exec_driver_sql(self, _statement):
            raw._read_bytes(1_000_000)
            pytest.fail('Oversized packet must be rejected before fetching')
        def invalidate(self):
            self.invalidated = True
    connection = Connection()
    with pytest.raises(ValueError, match='字节预算'):
        read_bounded(connection, 'SELECT data FROM events', max_rows=10, max_bytes=100, max_cell_bytes=100)
    assert not reads and connection.invalidated and raw._read_bytes == original


def test_cte_shadowing_cannot_bypass_table_scope_or_row_policy(app, client):
    from backend.services.sql_security import validate_query_tables
    with pytest.raises(PermissionError):
        validate_query_tables('WITH table_000 AS (SELECT 1 AS field_000) '
            'SELECT * FROM table_000 UNION ALL SELECT * FROM private.table_000', ['table_000'], 'sqlite')
    source, _ = register_fixture(app, client, rows=10)
    stored = app.extensions['meridian_db'].get('sources', source['id'], workspace_id='default')
    for table in stored['tables']:
        table['schema_name'] = 'main'
    app.extensions['meridian_db'].put('sources', stored, workspace_id='default')
    response = client.patch(f"/api/sources/{source['id']}",json={
        'row_policy':{'table':'table_000','column':'field_000','allow':{'*':['0']}}})
    assert response.status_code == 200
    queried = client.post('/api/query',json={'source_ids':[source['id']],
        'sql':'WITH table_000 AS (SELECT * FROM main.table_000) SELECT COUNT(*) AS n FROM table_000'})
    assert queried.status_code == 200, queried.get_json()
    assert queried.get_json()['result']['data'] == [{'n':1}]


def test_column_permissions_resolve_aliases_in_each_nested_scope(app, client):
    source, _ = register_fixture(app, client, tables=2, rows=10)
    response = client.patch(f"/api/sources/{source['id']}",json={
        'column_policies':{'table':'table_000','deny':{'*':['field_001']}}})
    assert response.status_code == 200
    queried = client.post('/api/query',json={'source_ids':[source['id']],
        'sql':'SELECT p.field_001 FROM table_000 AS p WHERE EXISTS '
              '(SELECT 1 FROM table_001 AS p WHERE p.field_001 IS NULL)'})
    assert queried.status_code == 403, queried.get_json()
    allowed = client.post('/api/query',json={'source_ids':[source['id']],
        'sql':'SELECT p.field_001 FROM table_001 AS p WHERE EXISTS '
              '(SELECT 1 FROM table_000 AS p WHERE p.field_000 IS NULL)'})
    assert allowed.status_code == 200, allowed.get_json()


def test_compact_time_metric_groups_and_filters_consistently(client):
    import io
    response = client.post('/api/sources/upload', data={'file':(io.BytesIO(
        b'day,amount\n20261001,100\n20261011,200\n20261012,300\n'), 'compact_dates.csv')},
        content_type='multipart/form-data')
    assert response.status_code == 201
    source = response.get_json()['items'][0]
    model = client.post('/api/semantic/models',json={'name':'compact_model','source_id':source['id'],
        'table':'compact_dates','dimensions':[{'name':'day','column':'day','type':'time','date_format':'yyyyMMdd'}],
        'measures':[{'name':'amount','column':'amount','aggregation':'sum'}]} )
    assert model.status_code == 201, model.get_json()
    metric = client.post('/api/semantic/metrics',json={'name':'dated_amount','model_id':model.get_json()['item']['id'],
        'measure':'amount','status':'approved'}).get_json()['item']
    response = client.post('/api/semantic/query',json={'metric':metric['id'],'group_by':['day'],
        'filters':[{'dimension':'day','op':'in','value':['2026-10-01','2026-10-11']}],
        'order_by':[{'field':'day','direction':'asc'}]})
    assert response.status_code == 200, response.get_json()
    assert [row['dated_amount'] for row in response.get_json()['result']['data']] == [100,200]
    assert "'20261001'" in response.get_json()['plan']['sql']


def test_native_date_decimal_and_binary_results_can_be_published(app, client, monkeypatch):
    from datetime import date, time as clock_time
    from decimal import Decimal
    import pandas as pd
    from backend.services import database_queries

    source, _ = register_fixture(app, client, rows=1)
    def typed_result(*_args, **_kwargs):
        return pd.DataFrame({'day':[date(2026,10,1)],'amount':[Decimal('123456789012345.001')],
            'clock':[clock_time(12,30)],'binary':[b'\x00\xff']}), 100
    monkeypatch.setattr(database_queries,'read_bounded',typed_result)
    response = client.post('/api/query',json={'source_ids':[source['id']],'sql':'SELECT * FROM table_000'})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()['result']['data'] == [{'day':'2026-10-01','amount':'123456789012345.001',
                                                    'clock':'12:30:00','binary':'0x00ff'}]


def test_finished_background_query_and_profile_reject_changed_permissions(app, client):
    source, _ = register_fixture(app, client, rows=10)
    query = client.post('/api/query/jobs',json={'source_ids':[source['id']],'sql':'SELECT * FROM table_000'}).get_json()['job']
    probe = client.post(f"/api/sources/{source['id']}/profile-jobs",json={'table':'table_000','mode':'aggregate'}).get_json()['job']
    assert wait_job(client,query['id'])['status'] == 'completed'
    assert wait_job(client,probe['id'])['status'] == 'completed'
    assert client.patch(f"/api/sources/{source['id']}",json={
        'column_policies':{'table':'table_000','deny':{'*':['field_001']}}}).status_code == 200
    assert client.get(f"/api/jobs/{query['id']}").status_code == 403
    assert client.get(f"/api/jobs/{probe['id']}").status_code == 403
    app.extensions['meridian_jobs'].shutdown()

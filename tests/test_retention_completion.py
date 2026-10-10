from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.agent.store import RunStore
from backend.services.lifecycle import RetentionWorker, expire_retention, save_settings


def age(database, collection, record_id, days=40):
    timestamp = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with database.transaction() as connection:
        connection.execute('UPDATE records SET updated_at=? WHERE collection=? AND id=?',
                           (timestamp, collection, record_id))
    return timestamp


def test_expired_content_roundtrips_through_actual_trash_without_deleting_files(app, client):
    database = app.extensions['meridian_db']
    session = client.post('/api/sessions', json={'name': '到期会话'}).get_json()['item']
    message = database.add_message(session['id'], 'user', '原消息')
    path = app.config['SETTINGS'].export_dir / 'retained.txt'
    path.write_text('成果正文', encoding='utf-8')
    artifact = database.put('artifacts', {'id': 'retained-artifact', 'path': str(path), 'filename': path.name,
                                        'actor_id': 'local-default', 'kind': 'upload'}, workspace_id='default')
    saved = database.put('saved_sessions', {'id': 'retained-save', 'owner_id': 'local-default', 'name': '保存会话'},
                         workspace_id='default')
    for collection, item in [('sessions', session), ('artifacts', artifact), ('saved_sessions', saved)]:
        old = age(database, collection, item['id'])
    with database.transaction() as connection:
        connection.execute('UPDATE messages SET created_at=? WHERE id=?', (old, message['id']))
    assert client.put('/api/lifecycle/settings', json={'retention_preset': 'custom', 'retention_custom_days': 30}).status_code == 200
    RetentionWorker(app).sweep()
    assert path.read_text(encoding='utf-8') == '成果正文'
    assert len(database.messages(session['id'])) == 1
    trash = client.get('/api/trash').get_json()['items']
    for collection, item in [('sessions', session), ('artifacts', artifact), ('saved_sessions', saved)]:
        assert any(row['id'] == item['id'] and row['collection'] == collection for row in trash)
        response = client.post(f"/api/trash/{collection}/{item['id']}/restore")
        assert response.status_code == 200, response.get_json()
    assert expire_retention(database, 'default') == {'sessions': 0, 'artifacts': 0, 'saved_sessions': 0}
    assert client.get(f"/api/sessions/{session['id']}").get_json()['messages'][0]['content'] == '原消息'
    assert any(row['id'] == artifact['id'] for row in client.get('/api/library').get_json()['items'])
    assert len([item for item in database.audit_entries('default', 100)
                if item['event_type'] == 'lifecycle.retention.archived']) == 3


@pytest.mark.parametrize('collection,workspace', [('artifacts', 'default'), ('artifacts', 'other'), ('query_results', 'default')])
def test_unknown_artifact_cleanup_protects_archived_and_shared_evidence_files(app, client, collection, workspace):
    database = app.extensions['meridian_db']
    path = app.config['SETTINGS'].export_dir / 'registered.txt'
    path.write_text('不能作为未知文件回收', encoding='utf-8')
    database.put(collection, {'id': 'registered-file', 'path': str(path), 'filename': path.name,
                              'actor_id': 'local-default', 'kind': 'upload'}, workspace_id=workspace)
    database.archive(collection, 'registered-file')
    preview = client.get('/api/lifecycle/artifacts/preview').get_json()['preview']
    assert not any(item['filename'] == path.name for item in preview['unknown_files'])
    response = client.post('/api/lifecycle/artifacts/unregistered/recycle',
                           json={'type': 'exports', 'relative_path': path.name})
    assert response.status_code == 400, response.get_json()
    assert path.is_file()
    if collection == 'artifacts' and workspace == 'default':
        assert client.post('/api/trash/artifacts/registered-file/restore').status_code == 200
        assert client.get('/api/library/registered-file/download').status_code == 200


def test_unknown_upload_cleanup_protects_archived_sources_in_other_workspaces(app, client):
    database = app.extensions['meridian_db']
    path = app.config['SETTINGS'].upload_dir / 'registered.csv'
    path.write_text('a\n1\n', encoding='utf-8')
    database.put('sources', {'id': 'other-upload', 'path': str(path)}, workspace_id='other')
    database.archive('sources', 'other-upload')
    preview = client.get('/api/lifecycle/uploads/preview').get_json()['preview']
    assert not any(item['filename'] == path.name for item in preview['samples'])
    response = client.post('/api/lifecycle/uploads/recycle',
                           json={'category': 'unknown_uploads', 'relative_path': path.name})
    assert response.status_code == 400, response.get_json()
    assert path.is_file()


@pytest.mark.parametrize('status', ['queued', 'running', 'waiting_input', 'waiting_approval', 'waiting_job', 'paused', 'cancelling'])
def test_old_incomplete_analysis_and_its_artifacts_are_kept(app, client, status):
    database = app.extensions['meridian_db']
    session = client.post('/api/sessions', json={'name': '尚未完成'}).get_json()['item']
    run, _ = RunStore(database).create_run(workspace_id='default', session_id=session['id'], actor_id='local-default',
                                         source_scope=[], allowed_tool_ids=[])
    artifact = database.put('artifacts', {'id': 'active-artifact', 'run_id': run['id']}, workspace_id='default')
    old = age(database, 'sessions', session['id'])
    age(database, 'artifacts', artifact['id'])
    with database.transaction() as connection:
        connection.execute('UPDATE agent_runs SET execution_status=?,updated_at=? WHERE id=?', (status, old, run['id']))
    save_settings(database, 'default', {'retention_preset': '7'})
    assert expire_retention(database, 'default') == {'sessions': 0, 'artifacts': 0, 'saved_sessions': 0}
    assert database.get('sessions', session['id'])
    assert database.get('artifacts', artifact['id'])


def test_recent_message_and_completed_run_extend_retention_and_forever_is_default(app, client):
    database = app.extensions['meridian_db']
    session = client.post('/api/sessions', json={'name': '近期消息'}).get_json()['item']
    age(database, 'sessions', session['id'])
    assert expire_retention(database, 'default')['sessions'] == 0
    save_settings(database, 'default', {'retention_preset': '14'})
    database.add_message(session['id'], 'user', '刚刚继续')
    assert expire_retention(database, 'default')['sessions'] == 0
    second = client.post('/api/sessions', json={'name': '刚结束的分析'}).get_json()['item']
    run, _ = RunStore(database).create_run(workspace_id='default', session_id=second['id'], actor_id='local-default',
                                         source_scope=[], allowed_tool_ids=[])
    with database.transaction() as connection:
        connection.execute("UPDATE agent_runs SET execution_status='finished' WHERE id=?", (run['id'],))
    age(database, 'sessions', second['id'])
    assert expire_retention(database, 'default')['sessions'] == 0


def test_retention_is_workspace_scoped_and_validates_real_integer_days(app, client):
    database = app.extensions['meridian_db']
    item = database.put('artifacts', {'id': 'other-artifact'}, workspace_id='other')
    age(database, 'artifacts', item['id'])
    save_settings(database, 'default', {'retention_preset': '7'})
    assert expire_retention(database, 'default')['artifacts'] == 0
    assert database.get('artifacts', item['id'], workspace_id='other')
    save_settings(database, 'other', {'retention_preset': '7'})
    RetentionWorker(app).sweep()
    assert database.get('artifacts', item['id'], workspace_id='other') is None
    for value in [0, 2.5, True, 3651]:
        assert client.put('/api/lifecycle/settings', json={'retention_preset': 'custom', 'retention_custom_days': value}).status_code == 400


def test_legacy_zero_day_setting_is_visible_and_does_not_silently_become_thirty_days(app, client):
    database = app.extensions['meridian_db']
    database.put('artifacts', {'id': 'legacy-artifact'}, workspace_id='default')
    age(database, 'artifacts', 'legacy-artifact')
    database.put('lifecycle_settings', {'id': 'lifecycle_default', 'retention_preset': 'custom', 'retention_custom_days': 0},
                 workspace_id='default')
    assert client.get('/api/lifecycle/settings').get_json()['settings']['retention_custom_days'] == 0
    assert expire_retention(database, 'default')['artifacts'] == 0
    assert database.get('artifacts', 'legacy-artifact')

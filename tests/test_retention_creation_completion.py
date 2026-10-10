from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.agent.store import RunStore
from backend.core.database import Database
from backend.services.lifecycle import expire_retention, save_settings


def _age_session(database, session_id):
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    with database.transaction() as connection:
        connection.execute(
            "UPDATE records SET updated_at=? WHERE collection='sessions' AND id=?",
            (old, session_id),
        )
    return old


def test_analysis_creation_rechecks_session_after_retention_scan(app, client, monkeypatch):
    database = app.extensions['meridian_db']
    session = client.post('/api/sessions', json={'name': '在到期边界继续'}).get_json()['item']
    _age_session(database, session['id'])
    save_settings(database, 'default', {'retention_preset': '7'})
    original_create = RunStore.create_run

    def expire_before_insert(store, **kwargs):
        assert expire_retention(database, 'default')['sessions'] == 1
        return original_create(store, **kwargs)

    # The request has already passed session authorization when the worker wins
    # the write lock. No run, context or message may be left behind afterward.
    with monkeypatch.context() as context:
        context.setattr(RunStore, 'create_run', expire_before_insert)
        response = client.post('/api/analyses', json={
            'session_id': session['id'], 'objective': '继续分析',
        })
    assert response.status_code == 400
    assert response.get_json()['error'] == '会话已进入回收站，请先恢复后重试'
    assert RunStore(database).list_runs('default', session_id=session['id']) == []
    assert database.list('analysis_context', workspace_id='default') == []
    assert database.list('skill_resolutions', workspace_id='default') == []
    assert database.messages(session['id']) == []
    assert client.post(f"/api/trash/sessions/{session['id']}/restore").status_code == 200
    retried = client.post('/api/analyses', json={
        'session_id': session['id'], 'objective': '恢复后继续分析',
    })
    assert retried.status_code == 201, retried.get_json()
    assert len(RunStore(database).list_runs('default', session_id=session['id'])) == 1


def test_creating_analysis_records_activity_without_overwriting_session_payload(app, client):
    database = app.extensions['meridian_db']
    session = client.post('/api/sessions', json={'name': '保留原会话设置'}).get_json()['item']
    database.patch('sessions', session['id'], {'temporary_instruction': '保留此配置'}, workspace_id='default')
    old = _age_session(database, session['id'])
    before = database.get('sessions', session['id'])
    run, created = RunStore(database).create_run(
        workspace_id='default', session_id=session['id'], actor_id='local-default',
        source_scope=[], allowed_tool_ids=[],
    )
    assert created
    assert database.get('sessions', session['id']) == before
    with database.connect() as connection:
        updated_at = connection.execute(
            "SELECT updated_at FROM records WHERE collection='sessions' AND id=?",
            (session['id'],),
        ).fetchone()['updated_at']
    assert datetime.fromisoformat(updated_at) > datetime.fromisoformat(old)
    # Even after the analysis has ended, the newly recorded activity protects
    # the conversation throughout its new retention period.
    with database.transaction() as connection:
        connection.execute(
            "UPDATE agent_runs SET execution_status='finished',updated_at=? WHERE id=?",
            (old, run['id']),
        )
    save_settings(database, 'default', {'retention_preset': '7'})
    assert expire_retention(database, 'default')['sessions'] == 0
    future = datetime.now(timezone.utc) + timedelta(days=8)
    assert expire_retention(database, 'default', now=future)['sessions'] >= 1
    assert database.get('sessions', session['id']) is None


def test_analysis_creation_rechecks_session_deleted_after_authorization(app, client, monkeypatch):
    database = app.extensions['meridian_db']
    session = client.post('/api/sessions', json={'name': '删除边界'}).get_json()['item']
    original_create = RunStore.create_run

    def delete_before_insert(store, **kwargs):
        assert client.delete(f"/api/sessions/{session['id']}").status_code == 200
        assert client.delete(
            f"/api/trash/sessions/{session['id']}", json={'confirm': True},
        ).status_code == 200
        return original_create(store, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(RunStore, 'create_run', delete_before_insert)
        response = client.post('/api/analyses', json={
            'session_id': session['id'], 'objective': '已授权但尚未插入',
        })
    assert response.status_code == 404
    assert response.get_json()['error'] == '会话不存在或已被永久删除，请重新创建会话后重试'
    assert database.get('sessions', session['id'], include_archived=True) is None
    assert RunStore(database).list_runs('default', session_id=session['id']) == []
    assert database.list('analysis_context', workspace_id='default') == []
    assert database.list('skill_resolutions', workspace_id='default') == []
    assert database.messages(session['id']) == []


def test_session_edit_reports_retention_conflict_and_can_retry_after_restore(app, client, monkeypatch):
    database = app.extensions['meridian_db']
    session = client.post('/api/sessions', json={'name': '编辑前'}).get_json()['item']
    _age_session(database, session['id'])
    save_settings(database, 'default', {'retention_preset': '7'})
    original_patch = Database.patch

    def expire_before_edit(store, collection, record_id, changes, **kwargs):
        if collection == 'sessions' and record_id == session['id']:
            assert expire_retention(database, 'default')['sessions'] == 1
        return original_patch(store, collection, record_id, changes, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(Database, 'patch', expire_before_edit)
        response = client.patch(f"/api/sessions/{session['id']}", json={'name': '编辑后'})
    assert response.status_code == 400
    assert response.get_json()['error'] == '会话已进入回收站，请先恢复后重试'
    assert database.get('sessions', session['id'], include_archived=True)['name'] == '编辑前'
    assert client.post(f"/api/trash/sessions/{session['id']}/restore").status_code == 200
    retried = client.patch(f"/api/sessions/{session['id']}", json={'name': '编辑后'})
    assert retried.status_code == 200
    assert retried.get_json()['item']['name'] == '编辑后'
    assert expire_retention(database, 'default')['sessions'] == 0


def test_run_creation_rejects_existing_foreign_workspace_session(app):
    database = app.extensions['meridian_db']
    database.put('sessions', {'id': 'foreign-session'}, workspace_id='other')
    with pytest.raises(PermissionError, match='会话不属于当前工作空间'):
        RunStore(database).create_run(
            workspace_id='default', session_id='foreign-session', actor_id='local-default',
            source_scope=[], allowed_tool_ids=[],
        )
    assert RunStore(database).list_runs('default') == []


def test_standalone_store_keeps_virtual_session_compatibility(app):
    store = RunStore(app.extensions['meridian_db'])
    run, created = store.create_run(
        workspace_id='default', session_id='virtual-session', actor_id='virtual-actor',
        source_scope=[], allowed_tool_ids=[],
    )
    assert created
    assert store.get_run(run['id'])['session_id'] == 'virtual-session'

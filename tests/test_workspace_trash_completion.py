from __future__ import annotations

import io


def _workspace_clients(app):
    clients = [app.test_client() for _ in range(3)]
    users = []
    for index, client in enumerate(clients):
        response = client.post('/api/auth/register', json={
            'email': f'trash-{index}@example.com', 'password': 'correct-horse', 'name': f'Member {index}',
        })
        assert response.status_code == 201, response.get_json()
        users.append(response.get_json()['user'])
    owner, editor, analyst = clients
    workspace = owner.post('/api/workspaces', json={'name': '回收站权限'}).get_json()['item']
    assert owner.post(f"/api/workspaces/{workspace['id']}/activate").status_code == 200
    for user, client, role in [(users[1], editor, 'editor'), (users[2], analyst, 'analyst')]:
        assert owner.post(f"/api/workspaces/{workspace['id']}/members", json={
            'email': user['email'], 'role': role,
        }).status_code == 201
        assert client.post(f"/api/workspaces/{workspace['id']}/activate").status_code == 200
    return owner, editor, analyst, workspace


def test_editor_can_restore_knowledge_but_analyst_cannot_see_it(app):
    owner, editor, analyst, _workspace = _workspace_clients(app)
    uploaded = owner.post('/api/knowledge/documents', data={
        'file': (io.BytesIO('恢复采购说明'.encode()), '采购说明.md'),
    }, content_type='multipart/form-data')
    assert uploaded.status_code == 201
    document_id = uploaded.get_json()['item']['id']
    entry = owner.post('/api/knowledge/entries', json={
        'name': '订单口径', 'type': 'business_rule', 'content': '只计已支付订单',
    }).get_json()['item']
    assert editor.delete(f'/api/knowledge/documents/{document_id}').status_code == 200
    assert editor.delete(f"/api/knowledge/entries/{entry['id']}").status_code == 200
    items = editor.get('/api/trash').get_json()['items']
    assert {document_id, entry['id']} <= {item['id'] for item in items}
    assert all('path' not in item and 'text' not in item and 'chunks' not in item for item in items)
    assert all(not item['can_delete'] for item in items)
    assert analyst.get('/api/trash').get_json()['items'] == []
    assert analyst.post(f'/api/trash/knowledge_documents/{document_id}/restore').status_code == 404
    assert editor.delete(f'/api/trash/knowledge_documents/{document_id}', json={'confirm': True}).status_code == 403
    assert editor.post(f'/api/trash/knowledge_documents/{document_id}/restore').status_code == 200
    assert editor.post(f"/api/trash/knowledge_entries/{entry['id']}/restore").status_code == 200
    assert document_id in {item['id'] for item in editor.get('/api/knowledge/documents').get_json()['items']}
    assert entry['id'] in {item['id'] for item in editor.get('/api/knowledge/entries').get_json()['items']}


def test_analysis_trash_is_private_even_from_workspace_owner(app):
    owner, editor, analyst, _workspace = _workspace_clients(app)
    run = analyst.post('/api/analyses', json={'objective': '个人分析恢复', 'source_ids': []}).get_json()['item']
    assert analyst.post(f"/api/analyses/{run['id']}/control", json={'action': 'cancel'}).status_code == 200
    assert analyst.delete(f"/api/analyses/{run['id']}").status_code == 200
    own = analyst.get('/api/trash?collection=agent_runs').get_json()['items']
    assert [item['id'] for item in own] == [run['id']]
    assert own[0]['can_restore'] is True and own[0]['can_delete'] is False
    assert owner.get('/api/trash?collection=agent_runs').get_json()['items'] == []
    assert editor.get('/api/trash?collection=agent_runs').get_json()['items'] == []
    assert owner.post(f"/api/analyses/{run['id']}/restore").status_code == 404
    assert analyst.post(f"/api/analyses/{run['id']}/restore").status_code == 200
    assert analyst.get(f"/api/analyses/{run['id']}").status_code == 200


def test_trash_rejects_arbitrary_collections_and_active_records(client, app):
    db = app.extensions['meridian_db']
    db.put('providers', {'id': 'secret-provider', 'name': 'private', 'api_key': 'secret'}, workspace_id='default')
    db.archive('providers', 'secret-provider', workspace_id='default')
    assert client.get('/api/trash?collection=providers').status_code == 400
    assert client.post('/api/trash/providers/secret-provider/restore').status_code == 404
    assert client.delete('/api/trash/providers/secret-provider', json={'confirm': True}).status_code == 404
    assert db.get('providers', 'secret-provider', include_archived=True)['archived_at']
    session = client.post('/api/sessions', json={'name': '活动会话'}).get_json()['item']
    assert client.delete(f"/api/trash/sessions/{session['id']}", json={'confirm': True}).status_code == 404
    assert client.get(f"/api/sessions/{session['id']}").status_code == 200


def test_library_trash_restore_and_confirmed_permanent_delete(client, app):
    response = client.post('/api/library', data={
        'file': (io.BytesIO(b'purchase note'), 'purchase.txt'),
    }, content_type='multipart/form-data')
    assert response.status_code == 201
    item = response.get_json()['items'][0]
    record = app.extensions['meridian_db'].get('artifacts', item['id'])
    from pathlib import Path

    path = Path(record['path'])
    assert client.delete(f"/api/library/{item['id']}").status_code == 200
    assert client.delete(f"/api/trash/artifacts/{item['id']}").status_code == 400
    assert path.is_file()
    assert client.post(f"/api/trash/artifacts/{item['id']}/restore").status_code == 200
    assert client.get(f"/api/library/{item['id']}/download").status_code == 200
    assert client.delete(f"/api/library/{item['id']}").status_code == 200
    assert client.delete(f"/api/trash/artifacts/{item['id']}", json={'confirm': True}).status_code == 200
    assert not path.exists()
    assert app.extensions['meridian_db'].get('artifacts', item['id'], include_archived=True) is None


def test_session_permanent_delete_preserves_analysis_recovery(client):
    run = client.post('/api/analyses', json={'objective': '原会话仍可恢复', 'source_ids': []}).get_json()['item']
    assert client.post(f"/api/analyses/{run['id']}/control", json={'action': 'cancel'}).status_code == 200
    assert client.delete(f"/api/analyses/{run['id']}").status_code == 200
    assert client.delete(f"/api/sessions/{run['session_id']}").status_code == 200
    sessions = client.get('/api/trash?collection=sessions').get_json()['items']
    assert next(item for item in sessions if item['id'] == run['session_id'])['can_delete'] is False
    assert client.delete(f"/api/trash/sessions/{run['session_id']}", json={'confirm': True}).status_code == 400
    assert client.post(f"/api/analyses/{run['id']}/restore").status_code == 200
    detail = client.get(f"/api/sessions/{run['session_id']}").get_json()
    assert any(item['content'] == '原会话仍可恢复' for item in detail['messages'])


def test_analysis_trash_explains_missing_source_before_restore(client, source):
    run = client.post('/api/analyses', json={
        'objective': '等待数据恢复的分析', 'source_ids': [source['id']],
    }).get_json()['item']
    assert client.post(f"/api/analyses/{run['id']}/control", json={'action': 'cancel'}).status_code == 200
    assert client.delete(f"/api/analyses/{run['id']}").status_code == 200
    assert client.delete(f"/api/sources/{source['id']}").status_code == 200
    item = client.get('/api/trash?collection=agent_runs').get_json()['items'][0]
    assert item['can_restore'] is False
    assert '先恢复分析使用的数据源' in item['restore_block_reason']
    assert client.post(f"/api/trash/sources/{source['id']}/restore").status_code == 200
    item = client.get('/api/trash?collection=agent_runs').get_json()['items'][0]
    assert item['can_restore'] is True and item['restore_block_reason'] == ''
    assert client.post(f"/api/analyses/{run['id']}/restore").status_code == 200


def test_active_runs_do_not_push_archived_analysis_out_of_trash(client, app):
    run = client.post('/api/analyses', json={'objective': '旧归档分析仍可恢复'}).get_json()['item']
    assert client.post(f"/api/analyses/{run['id']}/control", json={'action': 'cancel'}).status_code == 200
    assert client.delete(f"/api/analyses/{run['id']}").status_code == 200
    database = app.extensions['meridian_db']
    with database.transaction() as connection:
        record = dict(connection.execute('SELECT * FROM agent_runs WHERE id=?', (run['id'],)).fetchone())
        columns = list(record)
        rows = []
        for index in range(510):
            values = {**record, 'id': f'new-active-{index}', 'archived_at': None,
                      'execution_status': 'waiting_input', 'created_at': '2099-01-01T00:00:00Z'}
            rows.append(tuple(values[column] for column in columns))
        connection.executemany(
            f"INSERT INTO agent_runs({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
            rows,
        )
    items = client.get('/api/trash?collection=agent_runs').get_json()['items']
    assert [item['id'] for item in items] == [run['id']]
    assert items[0]['can_restore'] is True

from __future__ import annotations

from backend.services.security import SecretVault


def test_business_preview_and_execution_share_skill_lifecycle_but_editor_can_test_drafts(client, source):
    skill_id = 'review-preview-lifecycle'
    name = '核查草稿命中边界'
    question = f'@{name}'
    created = client.post('/api/skills', json={
        'id': skill_id, 'name': name, 'description': '测试业务预检与执行使用相同技能范围',
        'instruction': '读取授权数据并核查。', 'allowed_tools': ['query_data'],
        'triggers': [name], 'example_questions': [name], 'source_ids': [source['id']],
    })
    assert created.status_code == 201, created.get_json()

    def preview_ids():
        response = client.post('/api/skills/resolve', json={'question': question})
        assert response.status_code == 200
        data = response.get_json()
        return {item['id'] for item in data['selected'] + data['candidates']}

    def start_analysis():
        return client.post('/api/analyses', json={'objective': question, 'source_ids': [source['id']]})

    assert skill_id not in preview_ids()
    assert start_analysis().status_code == 400
    editor = client.post(f'/api/skills/{skill_id}/test', json={'question': question})
    assert editor.status_code == 200
    assert editor.get_json()['evaluation']['passed'] is True
    assert skill_id in {item['id'] for item in editor.get_json()['resolution']['selected']}
    assert client.post(f'/api/skills/{skill_id}/publish').status_code == 200
    assert skill_id in preview_ids()
    assert start_analysis().status_code == 201
    assert client.patch(f'/api/skills/{skill_id}', json={'status': 'disabled'}).status_code == 200
    assert skill_id not in preview_ids()
    assert start_analysis().status_code == 400
    assert client.post(f'/api/skills/{skill_id}/publish').status_code == 200
    assert skill_id in preview_ids()


def test_published_skill_preview_respects_revoked_resource_access(app, client, source):
    skill_id = 'review-preview-acl'
    created = client.post('/api/skills', json={
        'id': skill_id, 'name': '权限核查专用技能', 'description': '检查资源权限撤销后的预检与执行',
        'instruction': '只使用授权数据。', 'allowed_tools': ['query_data'],
        'triggers': ['权限核查专用技能'], 'source_ids': [source['id']],
    })
    assert created.status_code == 201
    assert client.post(f'/api/skills/{skill_id}/publish').status_code == 200
    app.extensions['meridian_db'].patch('sources', source['id'], {'authorized_user_ids': ['another-user']})
    data = client.post('/api/skills/resolve', json={'question': '@权限核查专用技能'}).get_json()
    assert skill_id not in {item['id'] for item in data['selected'] + data['candidates']}
    assert skill_id not in {item['id'] for item in client.get('/api/bootstrap').get_json()['skills']}
    response = client.post('/api/analyses', json={
        'objective': '@权限核查专用技能', 'source_ids': [source['id']],
    })
    assert response.status_code == 403


def test_source_pages_continue_past_an_empty_authorized_page(app, client):
    database = app.extensions['meridian_db']
    for index in range(101):
        database.put('sources', {
            'id': f'review-page-{index:03d}', 'name': f'分页数据源 {index}', 'kind': 'file',
            'status': 'ready', 'tables': [],
            **({'authorized_user_ids': ['another-user']} if index < 100 else {}),
        }, workspace_id='default')
    first = client.get('/api/sources').get_json()
    assert first['items'] == []
    assert first['next_cursor']
    second = client.get('/api/sources', query_string={'cursor': first['next_cursor']}).get_json()
    assert [item['id'] for item in second['items']] == ['review-page-100']
    assert second['next_cursor'] is None


def test_business_bootstrap_only_offers_published_and_authorized_skills(client):
    created = client.post('/api/skills', json={
        'id': 'review-draft', 'name': '未发布的审查技能', 'description': '核查发布前后的可见范围',
        'instruction': '读取已授权数据', 'allowed_tools': ['query_data'],
        'triggers': ['核查数据'], 'example_questions': ['核查数据是否完整'],
    })
    assert created.status_code == 201, created.get_json()
    assert 'review-draft' not in {item['id'] for item in client.get('/api/bootstrap').get_json()['skills']}
    assert client.post('/api/skills/review-draft/publish').status_code == 200
    assert 'review-draft' in {item['id'] for item in client.get('/api/bootstrap').get_json()['skills']}
    disabled = client.patch('/api/skills/review-draft', json={'status': 'disabled'})
    assert disabled.status_code == 200
    assert disabled.get_json()['item']['status'] == 'disabled'
    assert 'review-draft' not in {item['id'] for item in client.get('/api/bootstrap').get_json()['skills']}


def test_analysis_setup_uses_configured_credentials_and_workspace_scope(app, client, monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    database = app.extensions['meridian_db']
    key = SecretVault(app.config['VAULT_KEY']).seal({'api_key': 'isolated-test-key'})

    def ready():
        return next(item['done'] for item in client.get('/api/bootstrap').get_json()['onboarding']['steps']
                    if item['id'] == 'connect_model')

    assert ready() is False
    database.put('providers', {'id': 'review-other', 'enabled': True, 'credential': key}, workspace_id='other')
    assert ready() is False
    database.put('providers', {'id': 'review-model', 'enabled': False, 'credential': key}, workspace_id='default')
    assert ready() is False
    database.patch('providers', 'review-model', {'enabled': True}, workspace_id='default')
    assert ready() is True
    database.archive('providers', 'review-model')
    database.put('providers', {'id': 'environment-default', 'enabled': True, 'secret_source': 'environment'},
                 workspace_id='default')
    monkeypatch.setenv('OPENAI_API_KEY', 'isolated-environment-key')
    assert ready() is True
    from backend.services.product import onboarding_status

    with app.app_context():
        assert onboarding_status(database, 'other')['steps'][0]['done'] is True

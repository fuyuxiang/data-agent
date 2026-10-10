"""Live publication, draft editing, compatibility and source-scope regressions."""

import io
import json

import pytest

from backend.services.agent_definitions import agent_references, agent_source_ids, published_agent


def _create(client, source, **fields):
    response = client.post('/api/agents', json={
        'name': '线上助手', 'instruction': '使用万元口径', 'source_ids': [source['id']],
        'workspace_id': source.get('workspace_id', 'default'), **fields,
    })
    assert response.status_code == 201, response.get_json()
    return response.get_json()['item']


def _publish(client, item):
    response = client.post(f"/api/agents/{item['id']}/publish", json={'workspace_id': item.get('workspace_id', 'default')})
    assert response.status_code == 200, response.get_json()
    return response.get_json()['item']


def _live(app, item):
    database = app.extensions['meridian_db']
    return published_agent(database, database.get('agent_definitions', item['id'], workspace_id='default'))


def test_saving_a_published_agent_preserves_live_configuration_until_republish(app, client, source):
    item = _publish(client, _create(client, source, suggested_questions=['线上问题']))
    saved = client.patch(f"/api/agents/{item['id']}", json={
        'name': '草稿助手', 'instruction': '待验证的新口径', 'suggested_questions': ['草稿问题'],
        'published_version': 999, 'builtin': True, 'source_scope_mode': 'authorized',
    })
    assert saved.status_code == 200, saved.get_json()
    draft = saved.get_json()['item']
    assert draft['status'] == 'draft'
    assert draft['published_version'] == 1
    assert draft['has_unpublished_changes'] is True
    assert not draft.get('builtin')
    assert draft['source_scope_mode'] == 'bound'
    live = _live(app, item)
    assert live['instruction'] == '使用万元口径'
    assert live['name'] == '线上助手' and live['version'] == 1
    visible = next(a for a in client.get('/api/agents?view=published').get_json()['items'] if a['id'] == item['id'])
    assert visible['name'] == '线上助手'
    assert visible['suggested_questions'] == ['线上问题']
    bootstrap = next(a for a in client.get('/api/bootstrap').get_json()['agents'] if a['id'] == item['id'])
    assert bootstrap['status'] == 'published' and bootstrap['name'] == '线上助手'
    created = client.post('/api/analyses', json={
        'objective': '查销售', 'source_ids': [source['id']], 'agent_id': item['id'],
        'confirm_required': True,
    })
    assert created.status_code == 201, created.get_json()
    assert created.get_json()['item']['agent_version'] == 1
    _publish(client, draft)
    assert _live(app, item)['instruction'] == '待验证的新口径'
    assert _live(app, item)['version'] == 2


def test_rollback_creates_a_draft_without_replacing_the_live_version(app, client, source):
    item = _publish(client, _create(client, source))
    client.patch(f"/api/agents/{item['id']}", json={'instruction': '新版口径'})
    _publish(client, item)
    rolled = client.post(f"/api/agents/{item['id']}/rollback", json={'version': 1})
    assert rolled.status_code == 200, rolled.get_json()
    draft = rolled.get_json()['item']
    assert draft['instruction'] == '使用万元口径'
    assert draft['version'] == 3 and draft['published_version'] == 2
    assert draft['has_unpublished_changes']
    assert _live(app, item)['instruction'] == '新版口径'
    _publish(client, item)
    assert _live(app, item)['instruction'] == '使用万元口径'
    assert _live(app, item)['version'] == 3


def test_new_draft_cannot_spoof_a_live_publication_or_dynamic_scope(client, source):
    item = _create(client, source, status='published', published_version=1, builtin=True,
                   source_scope_mode='authorized', source_ids=[])
    assert item['status'] == 'draft' and item['published_version'] is None
    assert item['source_scope_mode'] == 'bound' and not item.get('builtin')
    assert item['id'] not in {a['id'] for a in client.get('/api/agents?view=published').get_json()['items']}
    assert client.post(f"/api/agents/{item['id']}/publish").status_code == 400


def test_legacy_published_definition_is_preserved_before_the_first_edit(app, client, source):
    database = app.extensions['meridian_db']
    item = database.put('agent_definitions', {
        'id': 'legacy-published', 'workspace_id': 'default', 'name': '旧库助手',
        'instruction': '旧线上说明', 'source_ids': [source['id']], 'skill_ids': [],
        'status': 'published', 'version': 7, 'visibility': 'workspace', 'created_by': 'local-default',
    })
    assert _live(app, item)['version'] == 7
    saved = client.patch('/api/agents/legacy-published', json={'instruction': '编辑说明'})
    assert saved.status_code == 200, saved.get_json()
    assert saved.get_json()['item']['published_version'] == 7
    assert _live(app, item)['instruction'] == '旧线上说明'
    assert database.get('agent_versions', 'legacy-published:7')['snapshot']['instruction'] == '旧线上说明'


def test_legacy_draft_uses_its_last_explicitly_published_snapshot(app, source):
    database = app.extensions['meridian_db']
    item = database.put('agent_definitions', {
        'id': 'legacy-draft', 'workspace_id': 'default', 'name': '未发布名称',
        'instruction': '未发布说明', 'source_ids': [source['id']], 'status': 'draft', 'version': 2,
        'visibility': 'workspace', 'created_by': 'local-default',
    })
    database.put('agent_versions', {
        'id': 'legacy-draft:1', 'workspace_id': 'default', 'agent_id': item['id'], 'version': 1,
        'snapshot': {**item, 'name': '已发布名称', 'instruction': '已发布说明', 'version': 1},
    })
    live = _live(app, item)
    assert live['name'] == '已发布名称' and live['instruction'] == '已发布说明'
    live['source_ids'].clear()
    assert _live(app, item)['source_ids'] == [source['id']]


def test_missing_legacy_bindings_remain_editable_and_can_be_removed(app, client, source):
    database = app.extensions['meridian_db']
    item = _create(client, source)
    database.patch('agent_definitions', item['id'], {
        'source_ids': [source['id'], 'removed-source'], 'knowledge_document_ids': ['removed-document'],
    })
    assert item['id'] in {a['id'] for a in client.get('/api/agents').get_json()['items']}
    removed = client.patch(f"/api/agents/{item['id']}", json={
        'source_ids': [source['id']], 'knowledge_document_ids': [],
    })
    assert removed.status_code == 200, removed.get_json()
    assert removed.get_json()['item']['knowledge_document_ids'] == []
    _publish(client, item)


def test_builtin_dynamic_scope_and_static_custom_scope_are_distinct(app, client, source):
    client.post('/api/demo/seed', json={})
    database = app.extensions['meridian_db']
    builtin = database.get('agent_definitions', 'agent-superskill')
    live = published_agent(database, builtin)
    assert live['source_scope_mode'] == 'authorized'
    assert source['id'] in agent_source_ids(database, live, 'local-default')
    created = client.post('/api/analyses', json={
        'objective': '销售额', 'agent_id': builtin['id'], 'source_ids': [source['id']],
        'confirm_required': True,
    })
    assert created.status_code == 201, created.get_json()
    custom = _publish(client, _create(client, source))
    demo_id = client.post('/api/demo/seed', json={}).get_json()['source']['id']
    denied = client.post('/api/analyses', json={
        'objective': '销售额', 'agent_id': custom['id'], 'source_ids': [demo_id], 'confirm_required': True,
    })
    assert denied.status_code == 403


def test_legacy_builtin_with_empty_binding_uses_authorized_dynamic_scope(app, source):
    database = app.extensions['meridian_db']
    item = database.put('agent_definitions', {
        'id': 'agent-superskill', 'workspace_id': 'default', 'name': '旧超级智能体',
        'status': 'published', 'version': 1, 'builtin': True, 'source_ids': [],
    })
    live = published_agent(database, item)
    assert live['source_scope_mode'] == 'authorized'
    assert source['id'] in agent_source_ids(database, live, 'local-default')


def _register(client, email):
    result = client.post('/api/auth/register', json={
        'email': email, 'password': 'correct-horse', 'name': email.split('@')[0],
    })
    assert result.status_code == 201, result.get_json()
    return result.get_json()['user']


def test_analyst_bootstrap_keeps_published_status_and_private_draft_visibility(app):
    owner, analyst = app.test_client(), app.test_client()
    _register(owner, 'publication-owner@example.com')
    user = _register(analyst, 'publication-analyst@example.com')
    workspace = owner.post('/api/workspaces', json={'name': '发布测试空间'}).get_json()['item']
    invited = owner.post(f"/api/workspaces/{workspace['id']}/members", json={
        'email': user['email'], 'role': 'analyst',
    })
    assert invited.status_code == 201, invited.get_json()
    analyst.post(f"/api/workspaces/{workspace['id']}/activate")
    source = owner.post('/api/sources/upload', data={
        'file': (io.BytesIO(b'region,sales\nA,10\n'), 'analyst-publication.csv'),
        'workspace_id': workspace['id'],
    }, content_type='multipart/form-data').get_json()['items'][0]
    public = _publish(owner, _create(owner, source))
    owner.patch(f"/api/agents/{public['id']}", json={'name': '私有草稿', 'visibility': 'private', 'workspace_id': workspace['id']})
    cards = analyst.get('/api/bootstrap').get_json()['agents']
    live = next(a for a in cards if a['id'] == public['id'])
    assert live['name'] == '线上助手' and live['status'] == 'published'
    private = _publish(owner, _create(owner, source, name='私有线上', visibility='private'))
    owner.patch(f"/api/agents/{private['id']}", json={'visibility': 'workspace', 'name': '准备共享', 'workspace_id': workspace['id']})
    assert private['id'] not in {a['id'] for a in analyst.get('/api/agents').get_json()['items']}
    assert private['id'] not in {a['id'] for a in analyst.get('/api/bootstrap').get_json()['agents']}
    run = analyst.post('/api/analyses', json={
        'objective': '查销售', 'agent_id': public['id'], 'source_ids': [source['id']],
        'confirm_required': True,
    })
    assert run.status_code == 201, run.get_json()
    database = app.extensions['meridian_db']
    owner_id = owner.get('/api/auth/me').get_json()['user']['id']
    restricted = database.put('sources', {
        **source, 'id': 'publication-restricted-source', 'authorized_user_ids': [owner_id],
    }, workspace_id=workspace['id'])
    builtin = database.put('agent_definitions', {
        'id': 'agent-superskill', 'workspace_id': workspace['id'], 'name': '权限动态助手',
        'builtin': True, 'source_ids': [], 'status': 'published', 'version': 1,
        'visibility': 'workspace', 'created_by': owner_id, 'source_scope_mode': 'authorized',
    }, workspace_id=workspace['id'])
    scope = agent_source_ids(database, published_agent(database, builtin), user['id'])
    assert source['id'] in scope and restricted['id'] not in scope
    denied = analyst.post('/api/analyses', json={
        'objective': '查询无权限数据', 'agent_id': builtin['id'], 'source_ids': [restricted['id']],
        'confirm_required': True,
    })
    assert denied.status_code == 403


def test_shared_live_agent_cannot_be_unpublished_by_editor_draft_visibility(app):
    owner, editor, other_editor = app.test_client(), app.test_client(), app.test_client()
    _register(owner, 'publisher@example.com')
    user = _register(editor, 'draft-editor@example.com')
    other_user = _register(other_editor, 'publication-observer@example.com')
    workspace = owner.post('/api/workspaces', json={'name': '发布权限空间'}).get_json()['item']
    owner.post(f"/api/workspaces/{workspace['id']}/members", json={'email': user['email'], 'role': 'editor'})
    owner.post(f"/api/workspaces/{workspace['id']}/members", json={'email': other_user['email'], 'role': 'editor'})
    editor.post(f"/api/workspaces/{workspace['id']}/activate")
    other_editor.post(f"/api/workspaces/{workspace['id']}/activate")
    source = owner.post('/api/sources/upload', data={
        'file': (io.BytesIO(b'name,value\nA,1\n'), 'publication-permissions.csv'),
        'workspace_id': workspace['id'],
    }, content_type='multipart/form-data').get_json()['items'][0]
    item = _create(editor, source)
    _publish(owner, item)
    saved = editor.patch(f"/api/agents/{item['id']}", json={
        'visibility': 'private', 'name': '私有草稿名称', 'instruction': '私有草稿口径',
    })
    assert saved.status_code == 200, saved.get_json()
    assert editor.post(f"/api/agents/{item['id']}/publish").status_code == 403
    assert editor.delete(f"/api/agents/{item['id']}").status_code == 403
    live = next(a for a in owner.get('/api/agents?view=published', headers={'X-Workspace-Id': workspace['id']}).get_json()['items'] if a['id'] == item['id'])
    assert live['visibility'] == 'workspace'
    headers = {'X-Workspace-Id': workspace['id']}
    governed = next(a for a in owner.get('/api/agents', headers=headers).get_json()['items'] if a['id'] == item['id'])
    assert governed['read_only_draft'] and governed['draft_private']
    assert governed['name'] == '线上助手' and governed['instruction'] == '使用万元口径'
    assert governed['version'] == governed['published_version'] == 1
    assert '私有草稿' not in json.dumps(governed, ensure_ascii=False)
    assert owner.patch(f"/api/agents/{item['id']}", headers=headers, json={'name': '越权修改'}).status_code == 404
    assert owner.post(f"/api/agents/{item['id']}/publish", headers=headers).status_code == 404
    assert item['id'] not in {a['id'] for a in other_editor.get('/api/agents').get_json()['items']}
    assert item['id'] in {a['id'] for a in other_editor.get('/api/agents?view=published').get_json()['items']}
    deleted = owner.delete(f"/api/agents/{item['id']}", headers=headers)
    assert deleted.status_code == 200, deleted.get_json()
    assert item['id'] not in {a['id'] for a in editor.get('/api/agents?view=published').get_json()['items']}


def test_private_author_can_share_a_draft_for_owner_approval_without_exposing_old_live_content(app):
    owner, author, other_editor = app.test_client(), app.test_client(), app.test_client()
    _register(owner, 'share-owner@example.com')
    user = _register(author, 'private-author@example.com')
    other = _register(other_editor, 'other-editor@example.com')
    workspace = owner.post('/api/workspaces', json={'name': '私有共享审批'}).get_json()['item']
    for client, member in ((author, user), (other_editor, other)):
        owner.post(f"/api/workspaces/{workspace['id']}/members", json={'email': member['email'], 'role': 'editor'})
        client.post(f"/api/workspaces/{workspace['id']}/activate")
    source = owner.post('/api/sources/upload', data={
        'workspace_id': workspace['id'],
        'file': (io.BytesIO(b'name,value\nA,1\n'), 'share-draft.csv'),
    }, content_type='multipart/form-data').get_json()['items'][0]
    item = _publish(author, _create(author, source, name='私有线上名称', visibility='private'))
    shared = author.patch(f"/api/agents/{item['id']}", json={'name': '待审批共享名称', 'visibility': 'workspace'})
    assert shared.status_code == 200, shared.get_json()
    headers = {'X-Workspace-Id': workspace['id']}
    draft = next(a for a in owner.get('/api/agents', headers=headers).get_json()['items'] if a['id'] == item['id'])
    assert draft['name'] == '待审批共享名称' and draft['published_visibility'] == 'private'
    assert item['id'] not in {a['id'] for a in owner.get('/api/agents?view=published', headers=headers).get_json()['items']}
    assert item['id'] not in {a['id'] for a in other_editor.get('/api/agents').get_json()['items']}
    hidden_version = owner.post(f"/api/agents/{item['id']}/rollback", headers=headers, json={'version': 1})
    assert hidden_version.status_code == 404
    denied = owner.post('/api/analyses', headers=headers, json={
        'objective': '读取私有线上', 'source_ids': [source['id']], 'agent_id': item['id'],
    })
    assert denied.status_code == 404
    assert author.post(f"/api/agents/{item['id']}/publish").status_code == 403
    _publish(owner, shared.get_json()['item'])
    visible = next(a for a in other_editor.get('/api/agents?view=published').get_json()['items'] if a['id'] == item['id'])
    assert visible['name'] == '待审批共享名称' and visible['visibility'] == 'workspace'
    hidden_history = owner.post(f"/api/agents/{item['id']}/rollback", headers=headers, json={'version': 1})
    assert hidden_history.status_code == 404


def _metric(client, source):
    created = client.post('/api/semantic/models', json={
        'name': 'publication-reference-model', 'source_id': source['id'], 'table': 'sales',
        'grain': '每行一个区域月记录', 'dimensions': [{'name': 'region', 'column': 'region', 'type': 'categorical'}],
        'measures': [{'name': 'sales_amount', 'column': 'sales', 'aggregation': 'sum'}],
    })
    assert created.status_code == 201, created.get_json()
    metric = client.post('/api/semantic/metrics', json={
        'name': 'publication_reference_sales', 'model_id': created.get_json()['item']['id'],
        'measure': 'sales_amount', 'status': 'approved',
    })
    assert metric.status_code == 201, metric.get_json()
    return metric.get_json()['item']


@pytest.mark.parametrize('resource', ['provider', 'metric', 'skill', 'source', 'mcp'])
def test_live_resource_dependencies_block_deletion_until_the_new_draft_is_published(app, client, source, resource):
    database = app.extensions['meridian_db']
    if resource == 'provider':
        provider = database.put('providers', {
            'id': 'publication-reference-provider', 'name': '线上模型',
            'model': 'review-model', 'base_url': 'https://model.example.test/v1',
        })
        field, value, cleared = 'provider_id', provider['id'], None
        delete_path = f"/api/providers/{value}"
    elif resource == 'metric':
        metric = _metric(client, source)
        field, value, cleared = 'metric_ids', [metric['id']], []
        delete_path = f"/api/semantic/metrics/{metric['id']}"
    elif resource == 'skill':
        skill = database.put('skills', {
            'id': 'skl-publication-reference', 'slug': 'publication-reference-skill',
            'name': '线上技能', 'source': 'workspace', 'status': 'published',
            'description': '读取数据结构', 'instruction': '读取所选数据表的结构。',
            'triggers': ['查看字段'], 'example_questions': ['字段有哪些'], 'allowed_tools': ['get_schema'],
            'version': 1, 'created_by': 'local-default',
        })
        field, value, cleared = 'skill_ids', [skill['slug']], []
        delete_path = f"/api/skills/{skill['slug']}"
    elif resource == 'mcp':
        server = database.put('mcp_servers', {
            'id': 'publication-reference-mcp', 'name': '线上工具', 'enabled': True,
            'status': 'connected', 'transport': 'streamable-http', 'tools': [],
        })
        field, value, cleared = 'mcp_server_ids', [server['id']], []
        delete_path = f"/api/mcp/servers/{server['id']}"
    else:
        replacement = client.post('/api/sources/upload', data={
            'file': (io.BytesIO(b'name,value\nB,2\n'), 'replacement-publication-source.csv'),
        }, content_type='multipart/form-data').get_json()['items'][0]
        field, value, cleared = 'source_ids', [source['id']], [replacement['id']]
        delete_path = f"/api/sources/{source['id']}"
    item = _publish(client, _create(client, source, **{field: value}))
    removed = client.patch(f"/api/agents/{item['id']}", json={field: cleared})
    assert removed.status_code == 200, removed.get_json()
    blocked = client.delete(delete_path)
    assert blocked.status_code == 400, blocked.get_json()
    _publish(client, item)
    deleted = client.delete(delete_path)
    assert deleted.status_code == 200, deleted.get_json()


def test_dynamic_builtin_can_bind_metrics_from_the_actors_authorized_sources(client, source):
    client.post('/api/demo/seed', json={})
    metric = _metric(client, source)
    saved = client.patch('/api/agents/agent-superskill', json={'metric_ids': [metric['id']]})
    assert saved.status_code == 200, saved.get_json()
    published = client.post('/api/agents/agent-superskill/publish')
    assert published.status_code == 200, published.get_json()
    assert published.get_json()['item']['source_scope_mode'] == 'authorized'


def test_dynamic_builtin_metric_dependency_keeps_its_source_until_republished_without_metric(client, source):
    client.post('/api/demo/seed', json={})
    metric = _metric(client, source)
    saved = client.patch('/api/agents/agent-superskill', json={'metric_ids': [metric['id']]})
    assert saved.status_code == 200, saved.get_json()
    assert client.post('/api/agents/agent-superskill/publish').status_code == 200
    path = f"/api/sources/{source['id']}"
    assert client.delete(path).status_code == 400
    assert client.patch('/api/agents/agent-superskill', json={'metric_ids': []}).status_code == 200
    assert client.delete(path).status_code == 400
    assert client.post('/api/agents/agent-superskill/publish').status_code == 200
    assert client.delete(path).status_code == 200


def test_dependency_guards_include_agents_beyond_the_catalog_page_limit(app, client):
    database = app.extensions['meridian_db']
    provider = database.put('providers', {'id': 'old-bound-provider', 'name': '保留模型'})
    keeper = database.put('agent_definitions', {
        'id': 'older-than-page-limit', 'name': '较旧但仍引用的草稿', 'status': 'draft',
        'published_version': None, 'provider_id': provider['id'],
    })
    timestamp = '2999-01-01T00:00:00+00:00'
    with database.transaction() as connection:
        connection.executemany(
            'INSERT INTO records(collection,id,workspace_id,payload,created_at,updated_at) VALUES(?,?,?,?,?,?)',
            [('agent_definitions', f'newer-agent-{index}', 'default', json.dumps({
                'id': f'newer-agent-{index}', 'workspace_id': 'default', 'name': '无引用的草稿',
                'status': 'draft', 'published_version': None,
            }), timestamp, timestamp) for index in range(5001)],
        )
    assert keeper['id'] not in {item['id'] for item in database.list('agent_definitions', limit=5000)}
    assert [item['id'] for item in agent_references(database, 'default', 'provider_id', provider['id'])] == [keeper['id']]
    assert client.delete(f"/api/providers/{provider['id']}").status_code == 400


@pytest.mark.parametrize('resource', ['provider', 'provider-zero', 'metric-draft', 'metric-deprecated', 'model-disabled', 'model-structure', 'skill', 'mcp', 'mcp-zero'])
def test_resource_deactivation_cannot_invalidate_a_live_agents_dependencies(app, client, source, monkeypatch, resource):
    database = app.extensions['meridian_db']
    if resource.startswith('provider'):
        monkeypatch.setattr('backend.services.models._provider_url', lambda value, **_kwargs: value)
        provider = database.put('providers', {
            'id': 'publication-disable-provider', 'name': '待停用模型',
            'model': 'review-model', 'base_url': 'https://model.example.test/v1', 'enabled': True,
        })
        field, binding, cleared = 'provider_id', provider['id'], None
        path, changes = f"/api/providers/{provider['id']}", {'enabled': 0 if resource.endswith('-zero') else False}
    elif resource == 'skill':
        skill = database.put('skills', {
            'id': 'skl-disable-reference', 'slug': 'disable-reference-skill', 'name': '待停用技能',
            'source': 'workspace', 'status': 'published', 'description': '读取字段',
            'instruction': '读取字段', 'triggers': ['查看字段'], 'example_questions': ['字段有哪些'],
            'allowed_tools': ['get_schema'], 'version': 1, 'created_by': 'local-default',
        })
        field, binding, cleared = 'skill_ids', [skill['slug']], []
        path, changes = f"/api/skills/{skill['slug']}", {'status': 'disabled'}
    elif resource.startswith('mcp'):
        server = database.put('mcp_servers', {
            'id': 'publication-disable-mcp', 'name': '待停用工具', 'enabled': True,
            'status': 'connected', 'transport': 'streamable-http', 'tools': [],
        })
        field, binding, cleared = 'mcp_server_ids', [server['id']], []
        path, changes = f"/api/mcp/servers/{server['id']}", {'enabled': 0 if resource.endswith('-zero') else False}
    else:
        metric = _metric(client, source)
        field, binding, cleared = 'metric_ids', [metric['id']], []
        if resource.startswith('model-'):
            path = f"/api/semantic/models/{metric['model_id']}"
            changes = {'enabled': False} if resource == 'model-disabled' else {'grain': '新口径'}
        else:
            path = f"/api/semantic/metrics/{metric['id']}"
            changes = {'status': resource.removeprefix('metric-')}
    item = _publish(client, _create(client, source, **{field: binding}))
    if resource.startswith('model-'):
        # Cosmetic edits retain approval and do not need a resource migration.
        cosmetic = client.patch(path, json={'description': '只修改说明'})
        assert cosmetic.status_code == 200, cosmetic.get_json()
        assert database.get('semantic_metrics', metric['id'])['status'] == 'approved'
    assert client.patch(path, json=changes).status_code == 400
    removed = client.patch(f"/api/agents/{item['id']}", json={field: cleared})
    assert removed.status_code == 200, removed.get_json()
    assert client.patch(path, json=changes).status_code == 400
    _publish(client, item)
    allowed = client.patch(path, json=changes)
    assert allowed.status_code == 200, allowed.get_json()


@pytest.mark.parametrize('resource,enabled', [
    ('provider', False), ('environment-default', False), ('mcp', False), ('provider', 0), ('mcp', 0),
])
def test_disabled_resources_cannot_be_bound_or_published_but_can_be_unlinked(app, client, source, resource, enabled):
    database = app.extensions['meridian_db']
    if resource == 'mcp':
        server = database.put('mcp_servers', {
            'id': 'disabled-bind-mcp', 'name': '失效工具', 'enabled': enabled,
            'status': 'connected', 'transport': 'streamable-http', 'tools': [],
        })
        field, binding, cleared = 'mcp_server_ids', [server['id']], []
    else:
        provider = database.put('providers', {
            'id': 'environment-default' if resource == 'environment-default' else 'disabled-bind-provider',
            'name': '失效模型', 'model': 'review-model', 'enabled': enabled,
        })
        field, binding, cleared = 'provider_id', provider['id'], None
    attempted = client.post('/api/agents', json={
        'name': '绑定停用资源', 'source_ids': [source['id']], field: binding,
    })
    assert attempted.status_code == 400, attempted.get_json()
    item = _create(client, source)
    # A pre-existing invalid definition must reject publication and remain repairable.
    database.patch('agent_definitions', item['id'], {field: binding})
    assert client.post(f"/api/agents/{item['id']}/publish").status_code == 400
    unlinked = client.patch(f"/api/agents/{item['id']}", json={field: cleared})
    assert unlinked.status_code == 200, unlinked.get_json()
    _publish(client, item)


@pytest.mark.parametrize('change', ['source-migration', 'filters', 'unit'])
def test_bound_metric_definition_changes_require_unlinking_and_republishing(app, client, source, change):
    database = app.extensions['meridian_db']
    metric = _metric(client, source)
    item = _publish(client, _create(client, source, metric_ids=[metric['id']]))
    path = f"/api/semantic/metrics/{metric['id']}"
    cosmetic = client.patch(path, json={'description': '补充指标说明', 'label': '可编辑展示名称'})
    assert cosmetic.status_code == 200, cosmetic.get_json()
    if change == 'source-migration':
        outside_source = client.post('/api/sources/upload', data={
            'file': (io.BytesIO(b'region,sales\nOutside,900\n'), 'sales.csv'),
        }, content_type='multipart/form-data').get_json()['items'][0]
        outside_model = client.post('/api/semantic/models', json={
            'name': 'outside-publication-scope', 'source_id': outside_source['id'], 'table': 'sales',
            'dimensions': [{'name': 'region', 'column': 'region', 'type': 'categorical'}],
            'measures': [{'name': 'sales_amount', 'column': 'sales', 'aggregation': 'sum'}],
        })
        assert outside_model.status_code == 201, outside_model.get_json()
        changes = {'model_id': outside_model.get_json()['item']['id']}
        assert outside_source['id'] not in _live(app, item)['source_ids']
    elif change == 'filters':
        changes = {'filters': [{'dimension': 'region', 'op': '=', 'value': 'North'}]}
    else:
        changes = {'unit': '万元'}
    assert client.patch(path, json=changes).status_code == 400
    assert database.get('semantic_metrics', metric['id'])['definition_fingerprint'] == metric['definition_fingerprint']
    removed = client.patch(f"/api/agents/{item['id']}", json={'metric_ids': []})
    assert removed.status_code == 200, removed.get_json()
    assert client.patch(path, json=changes).status_code == 400
    _publish(client, item)
    changed = client.patch(path, json=changes)
    assert changed.status_code == 200, changed.get_json()
    assert changed.get_json()['item']['status'] == 'approved'


def test_derived_metric_dependency_protects_atomic_name_and_definition_but_allows_display_edits(client, source):
    metric = _metric(client, source)
    derived = client.post('/api/semantic/metrics', json={
        'name': 'publication_derived_sales', 'model_id': metric['model_id'], 'metric_type': 'derived',
        'expression': f"{metric['name']} * 2", 'status': 'approved',
    })
    assert derived.status_code == 201, derived.get_json()
    path = f"/api/semantic/metrics/{metric['id']}"
    assert client.patch(path, json={'name': 'renamed_atomic_sales'}).status_code == 400
    assert client.patch(path, json={'unit': '万元'}).status_code == 400
    assert client.patch(path, json={'label': '展示名称', 'description': '解释当前口径'}).status_code == 200
    assert client.delete(f"/api/semantic/metrics/{derived.get_json()['item']['id']}").status_code == 200
    assert client.patch(path, json={'name': 'renamed_atomic_sales'}).status_code == 200

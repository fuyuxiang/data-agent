"""MCP configuration, owner approval and current runtime authorization."""

from pathlib import Path
import sys

import pytest

from backend.agent.store import RunStore
from backend.services.advanced_agent import available_formal_tools, build_executor


def _server(app):
    return app.extensions['meridian_db'].put('mcp_servers', {
        'id': 'mcp-closure', 'name': '已核实的业务查询', 'transport': 'http',
        'url': 'https://example.com/mcp', 'enabled': True, 'status': 'connected',
        'tools': [{'name': 'lookup', 'description': '查询客户信息', 'inputSchema': {'type': 'object'}}],
        'formal_read_tools': ['lookup'],
    })


@pytest.mark.parametrize('archived', [False, True])
def test_duplicate_mcp_create_does_not_overwrite_or_restore_existing_configuration(app, client, monkeypatch, archived):
    monkeypatch.setattr('backend.api.integration.validate_outbound_url', lambda value: value)
    server = _server(app)
    database = app.extensions['meridian_db']
    if archived:
        database.archive('mcp_servers', server['id'])
    attempted = client.post('/api/mcp/servers', json={
        'server_id': server['id'], 'name': '意外重复', 'url': 'https://other.example.com/mcp',
    })
    assert attempted.status_code == 400, attempted.get_json()
    current = database.get('mcp_servers', server['id'], include_archived=True)
    assert current['url'] == server['url'] and current['formal_read_tools'] == ['lookup']
    assert bool(current.get('archived_at')) is archived


def test_connection_changes_invalidate_discovery_and_require_retest_before_reapproval(app, client, monkeypatch):
    monkeypatch.setattr('backend.api.integration.validate_outbound_url', lambda value: value)
    server = _server(app)
    changed = client.patch(f"/api/mcp/servers/{server['id']}", json={'url': 'https://other.example.com/mcp'})
    assert changed.status_code == 200, changed.get_json()
    assert changed.get_json()['item']['status'] == 'configured'
    assert changed.get_json()['item']['tools'] == []
    assert changed.get_json()['item']['formal_read_tools'] == []
    denied = client.patch(f"/api/mcp/servers/{server['id']}", json={'formal_read_tools': ['lookup']})
    assert denied.status_code == 400
    assert '测试连接' in denied.get_json()['error']


def test_tools_endpoint_returns_saved_approval_and_safe_cosmetic_updates_preserve_it(app, client):
    server = _server(app)
    response = client.get(f"/api/mcp/servers/{server['id']}/tools")
    assert response.status_code == 200
    assert response.get_json()['formal_read_tools'] == ['lookup']
    changed = client.patch(f"/api/mcp/servers/{server['id']}", json={'name': '仅改名称'})
    assert changed.status_code == 200
    assert changed.get_json()['item']['status'] == 'connected'
    assert changed.get_json()['item']['formal_read_tools'] == ['lookup']


@pytest.mark.parametrize('revocation', ['tool', 'connection', 'membership'])
def test_formal_mcp_rechecks_authorization_before_calling_external_service(app, monkeypatch, revocation):
    server = _server(app)
    database = app.extensions['meridian_db']
    session = database.put('sessions', {'id': 'mcp-closure-session', 'owner_id': 'local-default', 'agent_allow_mcp': True})
    allowed = available_formal_tools(database, 'default', session['id'], [])
    tool_name = next(name for name in allowed if name.startswith('mcp__'))
    run, _ = RunStore(database).create_run(
        workspace_id='default', session_id=session['id'], actor_id='local-default',
        source_scope=[], allowed_tool_ids=allowed,
    )
    handler = build_executor(database, run).registry.get(tool_name).handler
    if revocation == 'membership':
        monkeypatch.setattr('backend.services.agent_tools.actor_role', lambda *_args: 'viewer')
    else:
        database.patch('mcp_servers', server['id'],
                       {'formal_read_tools': []} if revocation == 'tool' else {'status': 'configured'})

    class UnexpectedManager:
        def call_tool(self, *_args):
            raise AssertionError('revoked authorization must be checked before external I/O')

    monkeypatch.setattr('backend.services.mcp.get_mcp_manager', lambda: UnexpectedManager())
    with pytest.raises(PermissionError):
        handler({})


def test_owner_approval_exposes_discovered_tool_and_revocation_removes_it(app, client):
    server = _server(app)
    database = app.extensions['meridian_db']
    session = database.put('sessions', {'id': 'mcp-approval-session', 'owner_id': 'local-default', 'agent_allow_mcp': True})
    path = f"/api/mcp/servers/{server['id']}"
    assert client.patch(path, json={'formal_read_tools': []}).status_code == 200
    assert not any(name.startswith('mcp__') for name in available_formal_tools(database, 'default', session['id'], []))
    assert client.patch(path, json={'formal_read_tools': ['lookup']}).status_code == 200
    assert any(name.startswith('mcp__') for name in available_formal_tools(database, 'default', session['id'], []))
    assert client.patch(path, json={'formal_read_tools': ['unknown']}).status_code == 400


def test_local_mcp_connection_approval_and_formal_execution_are_a_complete_chain(app, client):
    fixture = Path(__file__).parent / 'browser' / 'fixtures' / 'mcp_readonly_server.py'
    created = client.post('/api/mcp/servers', json={
        'name': '本地协议测试', 'transport': 'stdio', 'command': sys.executable, 'args': [str(fixture)],
    })
    assert created.status_code == 201, created.get_json()
    server = created.get_json()['item']
    path = f"/api/mcp/servers/{server['id']}"
    try:
        assert client.patch(path, json={'formal_read_tools': ['lookup']}).status_code == 400
        tested = client.post(f'{path}/test')
        assert tested.status_code == 200, tested.get_json()
        assert client.patch(path, json={'formal_read_tools': ['lookup']}).status_code == 200
        database = app.extensions['meridian_db']
        session = database.put('sessions', {'id': 'real-mcp-session', 'owner_id': 'local-default', 'agent_allow_mcp': True})
        allowed = available_formal_tools(database, 'default', session['id'], [])
        tool_name = next(name for name in allowed if name.startswith('mcp__') and name.endswith('__lookup'))
        assert not any(name.endswith('__delete_record') for name in allowed)
        run, _ = RunStore(database).create_run(
            workspace_id='default', session_id=session['id'], actor_id='local-default',
            source_scope=[], allowed_tool_ids=allowed,
        )
        with app.app_context():
            value, _events = build_executor(database, run).registry.get(tool_name).handler({})
        assert value['content']['content'][0]['text'] == 'local fixture lookup result'
        assert value['external_unverified'] is True and value['completeness'] == 'unknown'
    finally:
        assert client.delete(path).status_code == 200


def test_published_agent_binding_and_research_skill_reach_only_its_approved_mcp(app, client, source):
    from backend.services.advanced_agent import _prepare_run_skills

    fixture = Path(__file__).parent / 'browser' / 'fixtures' / 'mcp_readonly_server.py'
    created = client.post('/api/mcp/servers', json={
        'name': '智能体绑定协议测试', 'transport': 'stdio', 'command': sys.executable, 'args': [str(fixture)],
    })
    server = created.get_json()['item']
    path = f"/api/mcp/servers/{server['id']}"
    try:
        assert client.post(f'{path}/test').status_code == 200
        assert client.patch(path, json={'formal_read_tools': ['lookup']}).status_code == 200
        other = _server(app)
        agent = client.post('/api/agents', json={
            'name': '研究测试助手', 'source_ids': [source['id']], 'skill_ids': ['deep-research'],
            'mcp_server_ids': [server['id']],
        }).get_json()['item']
        assert client.post(f"/api/agents/{agent['id']}/publish").status_code == 200
        response = client.post('/api/analyses', json={
            'objective': '研究测试资料', 'agent_id': agent['id'], 'confirm_required': True,
        })
        assert response.status_code == 201, response.get_json()
        database = app.extensions['meridian_db']
        run = RunStore(database).get_run(response.get_json()['item']['id'])
        session = database.get('sessions', run['session_id'])
        assert not session.get('agent_allow_mcp')
        allowed = run['allowed_tool_ids']
        tool_name = next(name for name in allowed if name.startswith('mcp__') and name.endswith('__lookup'))
        assert not any(name.startswith(f"mcp__{other['id']}__") for name in allowed)
        assert not any(name.endswith('__delete_record') for name in allowed)
        context = database.get('analysis_context', run['id'])
        with app.app_context():
            from backend.skills.executor import effective_tool_set
            from backend.skills.registry import SkillRegistry

            data_skill = SkillRegistry(database, 'default').get('data-analysis')
            assert tool_name not in effective_tool_set(data_skill, runtime_tools=allowed)
            blocks = _prepare_run_skills(database, run, context, allowed)
            skill_tools = set().union(*(set(block['allowed_tools']) for block in blocks))
            executor = build_executor(database, run)
            assert tool_name in skill_tools and tool_name in executor.registry.ids
            found, _ = executor.registry.get('search_mcp_tools').handler({'query': 'lookup'})
            assert [item['name'] for item in found['items']] == [tool_name]
            value, _ = executor.registry.get(tool_name).handler({})
        assert value['content']['content'][0]['text'] == 'local fixture lookup result'
        assert value['external_unverified'] is True
        forged = client.post('/api/analyses', json={
            'objective': '直接请求未绑定工具', 'source_ids': [source['id']],
            'skill_ids': ['deep-research'], 'mcp_server_ids': [server['id']], 'confirm_required': True,
        })
        assert forged.status_code == 201, forged.get_json()
        unbound = RunStore(database).get_run(forged.get_json()['item']['id'])
        assert not any(name.startswith('mcp__') for name in unbound['allowed_tool_ids'])
        child = client.post(f"/api/analyses/{run['id']}/branch", json={'mode': 'followup', 'prompt': '继续核对资料'})
        assert child.status_code == 201, child.get_json()
        assert tool_name in RunStore(database).get_run(child.get_json()['item']['id'])['allowed_tool_ids']
    finally:
        # The published binding is intentionally retained until explicitly removed.
        if 'agent' in locals():
            client.delete(f"/api/agents/{agent['id']}")
        assert client.delete(path).status_code == 200


@pytest.mark.parametrize('role', ['editor', 'analyst', 'viewer'])
def test_only_workspace_owner_can_grant_mcp_formal_tool_approval(app, role):
    owner, member = app.test_client(), app.test_client()
    owner.post('/api/auth/register', json={'email': 'mcp-owner@example.com', 'password': 'correct-horse'})
    member.post('/api/auth/register', json={'email': 'mcp-member@example.com', 'password': 'correct-horse'})
    workspace = owner.post('/api/workspaces', json={'name': '授权隔离'}).get_json()['item']
    assert owner.post(f"/api/workspaces/{workspace['id']}/members", json={
        'email': 'mcp-member@example.com', 'role': role,
    }).status_code == 201
    assert member.post(f"/api/workspaces/{workspace['id']}/activate").status_code == 200
    server = app.extensions['meridian_db'].put('mcp_servers', {
        'id': f'mcp-auth-{role}', 'name': '不可越权授权', 'transport': 'http', 'enabled': True,
        'status': 'connected', 'tools': [{'name': 'lookup'}], 'formal_read_tools': [],
    }, workspace_id=workspace['id'])
    denied = member.patch(f"/api/mcp/servers/{server['id']}", json={'formal_read_tools': ['lookup']})
    assert denied.status_code == 403
    assert app.extensions['meridian_db'].get('mcp_servers', server['id'])['formal_read_tools'] == []

"""Local JSON-RPC fixture; no network, model, or enterprise data access."""

import json
import sys


for line in sys.stdin:
    message = json.loads(line)
    if 'id' not in message:
        continue
    method = message.get('method')
    if method == 'initialize':
        result = {
            'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}},
            'serverInfo': {'name': 'closure-fixture', 'version': '1'},
        }
    elif method == 'tools/list':
        result = {'tools': [
            {'name': 'lookup', 'description': '查询测试资料', 'annotations': {'readOnlyHint': True},
             'inputSchema': {'type': 'object', 'properties': {}}},
            {'name': 'delete_record', 'description': '删除记录', 'annotations': {'destructiveHint': True},
             'inputSchema': {'type': 'object', 'properties': {}}},
        ]}
    elif method == 'tools/call':
        result = {'content': [{'type': 'text', 'text': 'local fixture lookup result'}], 'isError': False}
    else:
        result = {}
    print(json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}), flush=True)

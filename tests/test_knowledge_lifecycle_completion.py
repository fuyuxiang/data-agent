from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pandas as pd


def _document(client):
    response = client.post(
        "/api/knowledge/documents",
        data={"file": (io.BytesIO("采购复核必须使用订单原始凭据。".encode()), "采购复核.md")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 201
    return response.get_json()["item"]


def _agent(client, document_id, source_ids=None):
    response = client.post("/api/agents", json={
        "name": "采购复核智能体", "source_ids": source_ids or [],
        "knowledge_document_ids": [document_id],
    })
    assert response.status_code == 201
    return response.get_json()["item"]


def test_document_draft_reference_blocks_delete_and_disable(client, app):
    document = _document(client)
    agent = _agent(client, document["id"])
    references = client.get(f"/api/knowledge/documents/{document['id']}/references").get_json()["references"]
    assert references == [{"id": agent["id"], "name": agent["name"], "private": False, "scopes": ["draft"]}]
    listed = next(item for item in client.get("/api/knowledge/documents").get_json()["items"]
                  if item["id"] == document["id"])
    assert listed["references"] == references
    for method, payload in (("DELETE", None), ("PATCH", {"enabled": False})):
        response = client.open(f"/api/knowledge/documents/{document['id']}", method=method, json=payload)
        assert response.status_code == 409
        assert response.get_json()["references"] == references
        stored = app.extensions["meridian_db"].get("knowledge_documents", document["id"])
        assert stored and stored["enabled"] is True
    assert client.patch(f"/api/agents/{agent['id']}", json={"knowledge_document_ids": []}).status_code == 200
    assert client.delete(f"/api/knowledge/documents/{document['id']}").status_code == 200


def test_live_reference_remains_until_republish_and_history_does_not_block(client, source):
    document = _document(client)
    agent = _agent(client, document["id"], [source["id"]])
    assert client.post(f"/api/agents/{agent['id']}/publish").status_code == 200
    assert client.patch(f"/api/agents/{agent['id']}", json={"knowledge_document_ids": []}).status_code == 200
    references = client.get(f"/api/knowledge/documents/{document['id']}/references").get_json()["references"]
    assert references[0]["scopes"] == ["published"]
    assert client.delete(f"/api/knowledge/documents/{document['id']}").status_code == 409
    assert client.patch(f"/api/knowledge/documents/{document['id']}", json={"enabled": False}).status_code == 409
    assert client.post(f"/api/agents/{agent['id']}/publish").status_code == 200
    assert client.get(f"/api/knowledge/documents/{document['id']}/references").get_json()["references"] == []
    assert client.delete(f"/api/knowledge/documents/{document['id']}").status_code == 200


def test_document_reference_privacy_workspace_and_archived_agent_scope(client, app):
    document = _document(client)
    database = app.extensions["meridian_db"]
    private = database.put("agent_definitions", {
        "id": "agent_private_dependency", "name": "不得泄露的私有名称", "created_by": "someone-else",
        "visibility": "private", "status": "draft", "knowledge_document_ids": [document["id"]],
    })
    references = client.get(f"/api/knowledge/documents/{document['id']}/references").get_json()["references"]
    assert references == [{"id": None, "name": "其他成员的私有智能体", "private": True, "scopes": ["draft"]}]
    conflict = client.delete(f"/api/knowledge/documents/{document['id']}")
    assert conflict.status_code == 409
    assert "不得泄露" not in conflict.get_data(as_text=True)
    database.archive("agent_definitions", private["id"])
    database.put("agent_definitions", {
        "id": "agent_other_workspace", "name": "其他空间", "status": "draft",
        "knowledge_document_ids": [document["id"]],
    }, workspace_id="another-workspace")
    assert client.delete(f"/api/knowledge/documents/{document['id']}").status_code == 200


def test_legacy_import_delete_checks_the_same_agent_dependencies(client, app):
    stream = io.BytesIO()
    with pd.ExcelWriter(stream, engine="openpyxl") as writer:
        pd.DataFrame([{"指标名称": "采购订单金额", "定义": "已付款订单金额"}]).to_excel(writer, index=False)
    parsed = client.post("/api/knowledge/parse", data={
        "file": (io.BytesIO(stream.getvalue()), "采购口径.xlsx"),
    }, content_type="multipart/form-data").get_json()
    confirmed = client.post("/api/knowledge/confirm", json={
        "filename": parsed["filename"], "records": parsed["preview"],
    })
    assert confirmed.status_code == 200
    document_id = confirmed.get_json()["rag"]["document_id"]
    agent = _agent(client, document_id)
    database = app.extensions["meridian_db"]
    imported = next(item for item in database.list("knowledge_imports") if item["filename"] == parsed["filename"])
    original = Path(imported["path"])
    deleted = client.delete(f"/api/knowledge/files/{parsed['filename']}")
    assert deleted.status_code == 409
    assert original.is_file() and database.get("knowledge_documents", document_id)
    assert client.delete(f"/api/agents/{agent['id']}").status_code == 200
    assert client.delete(f"/api/knowledge/files/{parsed['filename']}").status_code == 200
    assert not database.get("knowledge_documents", document_id)


def test_unbound_document_disable_delete_and_restore_preserve_search_and_file(client, app):
    document = _document(client)
    database = app.extensions["meridian_db"]
    original = Path(database.get("knowledge_documents", document["id"])["path"])

    def found():
        return any(item["document_id"] == document["id"] for item in client.post(
            "/api/knowledge/search", json={"query": "采购复核"},
        ).get_json()["items"])

    assert found()
    assert client.patch(f"/api/knowledge/documents/{document['id']}", json={"enabled": "false"}).status_code == 400
    assert client.patch(f"/api/knowledge/documents/{document['id']}", json={"enabled": False}).status_code == 200
    assert not found()
    assert client.patch(f"/api/knowledge/documents/{document['id']}", json={"enabled": True}).status_code == 200
    assert found()
    assert client.delete(f"/api/knowledge/documents/{document['id']}").status_code == 200
    assert original.is_file() and not found()
    assert client.post(f"/api/trash/knowledge_documents/{document['id']}/restore").status_code == 200
    assert found()


def test_entry_disable_edit_delete_and_restore(client):
    entry = client.post("/api/knowledge/entries", json={
        "type": "business_rule", "name": "采购复核规则", "description": "必须使用订单原始凭据",
    }).get_json()["item"]

    def found():
        return any(item["document_id"] == entry["id"] for item in client.post(
            "/api/knowledge/search", json={"query": "采购复核"},
        ).get_json()["items"])

    assert found()
    assert client.patch(f"/api/knowledge/entries/{entry['id']}", json={"enabled": False}).status_code == 200
    edited = client.patch(f"/api/knowledge/entries/{entry['id']}", json={"name": "采购复核新版"})
    assert edited.status_code == 200 and edited.get_json()["item"]["enabled"] is False
    assert not found()
    assert client.patch(f"/api/knowledge/entries/{entry['id']}", json={"enabled": True}).status_code == 200
    assert client.delete(f"/api/knowledge/entries/{entry['id']}").status_code == 200
    assert not found()
    assert client.post(f"/api/trash/knowledge_entries/{entry['id']}/restore").status_code == 200
    assert found()


def test_inflight_index_rebuild_cannot_restore_deleted_document(client, app, monkeypatch):
    from backend.services import knowledge

    document = _document(client)
    database = app.extensions["meridian_db"]
    database.patch("knowledge_documents", document["id"], {"chunk_index": None})
    build = knowledge._build_chunk_index

    def archive_while_indexing(chunks, workspace_id):
        database.archive("knowledge_documents", document["id"])
        return build(chunks, workspace_id)

    monkeypatch.setattr(knowledge, "_build_chunk_index", archive_while_indexing)
    result = client.post("/api/knowledge/search", json={"query": "采购复核"})
    assert result.status_code == 200 and result.get_json()["items"] == []
    assert not database.get("knowledge_documents", document["id"])


def test_inflight_entry_edit_cannot_restore_deleted_entry(client, app, monkeypatch):
    from backend.services import knowledge

    entry = client.post("/api/knowledge/entries", json={
        "type": "context_note", "name": "采购复核背景", "content": "订单原始凭据",
    }).get_json()["item"]
    database = app.extensions["meridian_db"]
    embed = knowledge._embedding

    def archive_while_embedding(text, workspace_id):
        database.archive("knowledge_entries", entry["id"])
        return embed(text, workspace_id)

    monkeypatch.setattr(knowledge, "_embedding", archive_while_embedding)
    result = client.patch(f"/api/knowledge/entries/{entry['id']}", json={"name": "新版背景"})
    assert result.status_code == 404
    assert not database.get("knowledge_entries", entry["id"])


def test_knowledge_view_async_mutations_do_not_reintroduce_deleted_results_or_duplicate_requests():
    script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source = fs.readFileSync('frontend/src/views/admin-knowledge.js', 'utf8')
  .replace(/^import .*$/gm, '').replace('export const KnowledgeView', 'const KnowledgeView')
  + '\nthis.KnowledgeView = KnowledgeView;';
function view(actions = {}) {
  const context = { Icon: {}, EmptyState: {}, Modal: {}, SearchInput: {}, Status: {}, Switch: {}, Tabs: {},
    canAdmin: { value: true }, state: {}, toast() {}, actions: {
      get: async () => ({ references: [] }), remove: async () => ({ ok: true }),
      patch: async () => ({ item: { enabled: false } }), ...actions,
    } };
  vm.runInNewContext(source, context);
  return { ...context.KnowledgeView.data(), ...context.KnowledgeView.methods };
}
for (const mutation of ['removeDocument', 'removeEntry', 'toggle', 'toggleDocument']) {
  for (const hasPendingSearch of [false, true]) {
    let finishSearch;
    const item = { id: 'deleted', name: '采购复核', enabled: true, references: [] };
    const v = view({ post: () => new Promise(resolve => { finishSearch = resolve; }) });
    Object.assign(v, { searchQuery: '采购', entries: [item], documents: [item],
      entryDeleteTarget: item, documentDeleteTarget: item, documentReferencesChecked: true,
      results: [{ document_id: item.id }, { document_id: 'retained' }] });
    const pendingSearch = hasPendingSearch ? v.search() : null;
    if (hasPendingSearch) assert.equal(v.results.length, 0, 'a new search clears stale results');
    await v[mutation](item);
    if (hasPendingSearch) {
      assert.equal(v.results.length, 0, mutation);
      finishSearch({ items: [{ document_id: item.id }] });
      await pendingSearch;
      assert.equal(v.results.length, 0, 'a late search cannot restore removed or disabled content');
    } else {
      assert.equal(v.results.length, 1, mutation);
      assert.equal(v.results[0].document_id, 'retained', mutation);
    }
    assert.equal(v.searching, false, mutation);
  }
}
for (const [target, open, remove, close, deleting, error] of [
  ['entryDeleteTarget', 'openEntryDelete', 'removeEntry', 'closeEntryDelete', 'deletingEntry', 'entryDeleteError'],
  ['documentDeleteTarget', 'openDocumentDelete', 'removeDocument', 'closeDocumentDelete', 'deletingDocument', 'documentDeleteError'],
]) {
  let finishDelete, requests = 0;
  const v = view({ remove: () => { requests++; return new Promise(resolve => { finishDelete = resolve; }); } });
  const item = { id: 'one', name: '采购复核' };
  v.entries = [item]; v.documents = [item];
  await v[open](item);
  assert.equal(requests, 0, 'opening the confirmation cannot delete');
  const pending = v[remove]();
  await v[remove]();
  v[close]();
  assert.equal(requests, 1, 'duplicate confirmation is ignored');
  assert.equal(v[target], item, 'pending deletion cannot be dismissed');
  assert.equal(v[deleting], true);
  finishDelete({ ok: true });
  await pending;
  assert.equal(v[target], null);
  assert.equal(v[deleting], false);
  v.entries = [item]; v.documents = [item];
  const failed = view({ remove: async () => { throw new Error('保留文档'); } });
  failed.entries = [item]; failed.documents = [item];
  await failed[open](item);
  await failed[remove]();
  assert.equal(failed[target], item);
  assert.equal(failed[error], '保留文档');
  assert.equal(failed[deleting], false);
  assert.equal(failed.entries.length, 1);
}
let calls = 0;
const referenced = view({ get: async () => ({ references: [{ id: 'a', name: '智能体', scopes: ['published'] }] }),
  remove: async () => { calls++; return {}; } });
await referenced.openDocumentDelete({ id: 'bound', name: '已绑定文档' });
await referenced.removeDocument();
assert.equal(calls, 0, 'a referenced document cannot be confirmed');
console.log('knowledge view lifecycle and async checks passed');
"""
    result = subprocess.run(
        ["node", "--input-type=module"], input=script, text=True, capture_output=True,
        cwd=Path(__file__).resolve().parent.parent, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

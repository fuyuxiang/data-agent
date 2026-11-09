from __future__ import annotations

import hashlib
import secrets
import shutil
import sys
import tarfile
from dataclasses import replace
from email.message import Message
from io import BytesIO
from pathlib import Path
from urllib.request import HTTPSHandler, ProxyHandler, build_opener
from urllib.response import addinfourl

import pytest

from backend.api.identity import PASSWORD_ITERATIONS, _password_hash
from scripts import verify_deployment
from scripts.package_release import source_member, write_release_archive


def test_release_filters_appledouble_before_build_and_when_writing_archive(tmp_path):
    references = tmp_path / "input/skills/attribution/references"
    references.mkdir(parents=True)
    document = references / "decomposition.md"
    document.write_text("# 归因", encoding="utf-8")
    (references / "._decomposition.md").write_bytes(b"\x00\x05\x16\x07")
    (references / "._directory").mkdir()
    (references / "._directory/file.md").write_bytes(b"metadata")

    source_archive = tmp_path / "source.tar"
    with tarfile.open(source_archive, "w") as archive:
        archive.add(tmp_path / "input", arcname=".")
    source = tmp_path / "source"
    with tarfile.open(source_archive) as archive:
        archive.extractall(source, filter=source_member)
    assert not list(source.rglob("._*"))

    output = tmp_path / "release.tar.gz"
    dist = source / "frontend/dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_bytes(b"<html></html>")
    (dist / "._index.html").write_bytes(b"\x00\x05\x16\x07")
    commit = "a" * 40
    write_release_archive(source, output, commit)

    with tarfile.open(output) as archive:
        paths = {member.name for member in archive.getmembers() if member.isfile()}
        assert paths == {
            "./.release-commit", "./.release-sha256", "./frontend/dist/index.html",
            "./skills/attribution/references/decomposition.md",
        }
        assert archive.extractfile("./.release-commit").read() == f"{commit}\n".encode()
        manifest = archive.extractfile("./.release-sha256").read().decode()
        for path in paths - {"./.release-sha256"}:
            digest = hashlib.sha256(archive.extractfile(path).read()).hexdigest()
            assert f"{digest}  {path.removeprefix('./')}\n" in manifest


def test_deployment_verifier_requires_authenticated_bootstrap(app, monkeypatch, tmp_path, capsys):
    origin = "https://deploy.example.test"
    skill_dir = tmp_path / "skills"
    source = Path(__file__).resolve().parents[1] / "skills/attribution"
    shutil.copytree(source, skill_dir / "attribution")
    metadata = skill_dir / "attribution/references/._decomposition.md"
    metadata.write_bytes(b"\x00\x05\x16\x07\xa3")
    app.config.update(
        TESTING=False, SESSION_COOKIE_SECURE=True,
        SETTINGS=replace(app.config["SETTINGS"], skill_dir=skill_dir),
    )
    database = app.extensions["meridian_db"]
    password = secrets.token_urlsafe(24)
    salt, digest = _password_hash(password)
    user = database.create_user({
        "id": "deploy-check", "username": "deploy-check", "email": "deploy-check@example.test",
        "password_salt": salt, "password_hash": digest, "password_iterations": PASSWORD_ITERATIONS,
    })
    database.put("workspace_members", {"id": "deploy-check-default", "user_id": user["id"], "role": "viewer"})
    client = app.test_client(use_cookies=False)
    assert client.get("/api/bootstrap", base_url=origin).status_code == 401

    class AppHTTPSHandler(HTTPSHandler):
        def https_open(self, request):
            result = client.open(
                request.selector, base_url=origin, method=request.get_method(),
                headers=dict(request.header_items()), data=request.data,
            )
            headers = Message()
            for key, value in result.headers:
                headers[key] = value
            response = addinfourl(BytesIO(result.data), headers, request.full_url, result.status_code)
            response.msg = result.status
            return response

    monkeypatch.setattr(
        verify_deployment, "build_opener", lambda *handlers: build_opener(ProxyHandler({}), AppHTTPSHandler(), *handlers),
    )
    monkeypatch.setenv("MERIDIAN_DEPLOY_USERNAME", user["username"])
    monkeypatch.setenv("MERIDIAN_DEPLOY_PASSWORD", password)
    monkeypatch.setattr(sys, "argv", ["verify_deployment.py", origin])
    with pytest.raises(SystemExit) as failure:
        verify_deployment.main()
    assert failure.value.code == 1
    error = capsys.readouterr().err
    assert "/api/bootstrap 返回 HTTP 500（请求 ID " in error
    assert password not in error
    metadata.unlink()
    verify_deployment.main()
    assert "/api/bootstrap 均返回 HTTP 200，工作空间 default" in capsys.readouterr().out

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _module(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "packaging" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


build_staging = _module("build_staging").build_staging
desktop_launcher = _module("desktop_launcher")


def test_desktop_staging_is_allowlisted_and_excludes_runtime_data(tmp_path):
    destination = tmp_path / "staging"
    manifest = build_staging(ROOT, destination)
    paths = {item["path"] for item in manifest["files"]}
    assert "app.py" in paths
    assert "packaging/desktop_launcher.py" in paths
    assert "THIRD_PARTY_NOTICES.md" in paths
    assert "frontend/vendor/echarts-china.min.js" in paths
    assert not any(path.startswith("storage/") for path in paths)
    assert not any(".env" in path for path in paths)


def test_desktop_launcher_uses_local_port_and_platform_data_directory():
    assert desktop_launcher.user_data_dir().is_absolute()
    assert desktop_launcher.SERVICE_ID == "meridian-analytics-workbench"

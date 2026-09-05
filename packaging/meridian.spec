# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir bundle generated only from an audited staging tree."""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, copy_metadata


raw_staging = os.environ.get("MERIDIAN_STAGING_ROOT", "").strip()
if not raw_staging:
    raise SystemExit("MERIDIAN_STAGING_ROOT must point to the audited staging directory")
STAGING = Path(raw_staging).resolve(strict=True)
ENTRY = STAGING / "packaging" / "desktop_launcher.py"
if not ENTRY.is_file() or (STAGING / "storage").exists():
    raise SystemExit("invalid desktop packaging staging tree")
sys.path.insert(0, str(STAGING))


def tree(relative):
    root = STAGING / relative
    return [
        (str(item), str(item.parent.relative_to(STAGING)))
        for item in sorted(root.rglob("*"))
        if item.is_file() and "__pycache__" not in item.parts and item.suffix != ".pyc"
    ]


datas = tree("frontend") + tree("skills") + tree("deploy") + [
    (str(STAGING / "README.md"), "."),
    (str(STAGING / "THIRD_PARTY_NOTICES.md"), "."),
]
for package in ("pmdarima", "tokenizers", "pyecharts"):
    datas += copy_metadata(package)

hiddenimports = collect_submodules("backend") + [
    "duckdb", "openpyxl", "xlrd", "pymysql", "psycopg2", "pyodbc",
    "sqlalchemy.dialects.mysql.pymysql", "sqlalchemy.dialects.postgresql.psycopg2",
    "sqlalchemy.dialects.mssql.pyodbc", "statsmodels.tsa.api", "pmdarima", "tokenizers",
]
hiddenimports += collect_submodules("pyecharts")

a = Analysis(
    [str(ENTRY)], pathex=[str(STAGING)], binaries=[], datas=datas,
    hiddenimports=sorted(set(hiddenimports)), hookspath=[], hooksconfig={},
    runtime_hooks=[], excludes=["pytest"], noarchive=False, optimize=1,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="MeridianAnalyticsWorkbench",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=False,
)
coll = COLLECT(
    exe, a.binaries, a.datas, strip=False, upx=False, name="MeridianAnalyticsWorkbench",
)
if sys.platform == "darwin":
    app = BUNDLE(
        coll, name="Meridian Analytics Workbench.app",
        bundle_identifier="com.meridian.analytics.workbench",
        info_plist={"NSHighResolutionCapable": True, "LSMinimumSystemVersion": "12.0"},
    )

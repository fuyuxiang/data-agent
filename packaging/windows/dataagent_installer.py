from __future__ import annotations

import os
import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


APP_NAME = "DataAgent"


def _resource_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent


def _local_app_data() -> Path:
    base = os.getenv("LOCALAPPDATA")
    if base:
        return Path(base)
    return Path.home() / "AppData" / "Local"


def _shortcut_script(target: Path) -> str:
    return rf"""
$W = New-Object -ComObject WScript.Shell
$Target = '{str(target)}'
$Desktop = [Environment]::GetFolderPath('Desktop')
$Start = [Environment]::GetFolderPath('StartMenu')
$Links = @(
  (Join-Path $Desktop 'DataAgent.lnk'),
  (Join-Path $Start 'Programs\DataAgent.lnk')
)
foreach ($Path in $Links) {{
  $S = $W.CreateShortcut($Path)
  $S.TargetPath = $Target
  $S.WorkingDirectory = Split-Path $Target
  $S.IconLocation = $Target
  $S.Description = 'DataAgent 本地智能分析平台'
  $S.Save()
}}
"""


def _create_shortcuts(target: Path) -> None:
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            _shortcut_script(target),
        ],
        check=True,
    )


def _write_uninstaller(install_dir: Path) -> None:
    uninstall = install_dir / "Uninstall DataAgent.cmd"
    uninstall.write_text(
        """@echo off
setlocal
taskkill /IM DataAgent.exe /F >nul 2>nul
del "%USERPROFILE%\\Desktop\\DataAgent.lnk" >nul 2>nul
del "%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\DataAgent.lnk" >nul 2>nul
cd /d "%TEMP%"
rmdir /S /Q "%LOCALAPPDATA%\\Programs\\DataAgent"
endlocal
""",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Install DataAgent")
    parser.add_argument("--install-dir", type=Path, default=None)
    parser.add_argument("--no-shortcuts", action="store_true")
    parser.add_argument("--no-launch", action="store_true")
    args = parser.parse_args()

    source = _resource_root() / "DataAgent-app.zip"
    if not source.is_file():
        raise FileNotFoundError(f"安装包缺少 DataAgent-app.zip：{source}")

    install_dir = args.install_dir or (_local_app_data() / "Programs" / APP_NAME)
    subprocess.run(["taskkill", "/IM", "DataAgent.exe", "/F"], check=False, capture_output=True)
    if install_dir.exists():
        shutil.rmtree(install_dir)
    install_dir.mkdir(parents=True, exist_ok=True)
    target = install_dir / "DataAgent.exe"
    with zipfile.ZipFile(source) as archive:
        archive.extractall(install_dir)
    if not target.is_file():
        raise FileNotFoundError(f"安装后未找到 DataAgent.exe：{target}")
    _write_uninstaller(install_dir)
    if not args.no_shortcuts:
        _create_shortcuts(target)

    print(f"{APP_NAME} 已安装到：{install_dir}")
    if not args.no_shortcuts:
        print("已创建桌面和开始菜单快捷方式。")
    if not args.no_launch:
        subprocess.Popen([str(target)], cwd=str(install_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

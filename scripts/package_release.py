#!/usr/bin/env python3
"""Build a Linux deployment archive from a committed source revision."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]


def is_appledouble(name: str) -> bool:
    return any(part.startswith("._") for part in PurePosixPath(name).parts)


def source_member(member: tarfile.TarInfo, destination: str) -> tarfile.TarInfo | None:
    return None if is_appledouble(member.name) else tarfile.data_filter(member, destination)


def release_member(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
    return None if is_appledouble(member.name) else member


def write_release_archive(source: Path, output: Path, commit: str) -> None:
    (source / ".release-commit").write_text(f"{commit}\n", encoding="utf-8")
    manifest = source / ".release-sha256"
    with manifest.open("w", encoding="utf-8", newline="\n") as checksums:
        for file in sorted(source.rglob("*")):
            relative = file.relative_to(source).as_posix()
            if file == manifest or not file.is_file() or is_appledouble(relative):
                continue
            with file.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            escaped = relative.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")
            prefix = "\\" if relative != escaped else ""
            checksums.write(f"{prefix}{digest}  {escaped}\n")

    # Python's tar writer does not synthesize AppleDouble files from extended attributes.
    with tarfile.open(output, "w:gz") as archive:
        archive.add(source, arcname=".", filter=release_member)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", default="HEAD", help="Git commit, branch or tag to package")
    parser.add_argument("--output", required=True, type=Path, help="Output .tar.gz path")
    args = parser.parse_args()
    git = shutil.which("git")
    node = shutil.which("node")
    if not git or not node:
        parser.error("需要安装 Git 和 Node.js")

    commit = subprocess.check_output(  # noqa: S603
        [git, "rev-parse", "--verify", "--end-of-options", f"{args.revision}^{{commit}}"],
        cwd=ROOT, text=True,
    ).strip()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".data-agent-release-", dir=output.parent) as temporary:
        staging = Path(temporary)
        source_archive = staging / "source.tar"
        source = staging / "source"
        subprocess.run(  # noqa: S603
            [git, "archive", "--format=tar", f"--output={source_archive}", commit], cwd=ROOT, check=True,
        )
        with tarfile.open(source_archive, "r:") as archive:
            archive.extractall(source, filter=source_member)  # noqa: S202 - source_member applies tarfile.data_filter
        subprocess.run([node, "scripts/build.mjs"], cwd=source, check=True)  # noqa: S603

        candidate = staging / "release.tar.gz"
        write_release_archive(source, candidate, commit)
        candidate.replace(output)
    print(f"Packaged {commit} -> {output}")


if __name__ == "__main__":
    main()

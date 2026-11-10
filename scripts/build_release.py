#!/usr/bin/env python3
"""Build the Linux source bundle and the native Fedora RPM without root access."""
from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SOURCE_PARTS = ["main.py", "requirements.txt", "requirements-runtime.txt", "README.md",
                "INSTALL_LINUX.md", "install.sh", "ytrss", "tests", "docs", "packaging", "scripts"]


def version() -> str:
    tree = ast.parse((ROOT / "ytrss/__init__.py").read_text())
    for item in tree.body:
        if isinstance(item, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__version__" for t in item.targets):
            return ast.literal_eval(item.value)
    raise RuntimeError("Application version was not found")


def source_files():
    for part in SOURCE_PARTS:
        path = ROOT / part
        for file in sorted(path.rglob("*")) if path.is_dir() else [path]:
            if file.is_file() and "__pycache__" not in file.parts and file.suffix != ".pyc":
                yield file


def build_source(output: Path, app_version: str) -> Path:
    archive = output / f"youtube-feed-reader-{app_version}-linux-source.tar.gz"
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", (ROOT / "ytrss/__init__.py").stat().st_mtime))
    with archive.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as bundle:
            for file in source_files():
                relative = file.relative_to(ROOT)
                data = file.read_bytes()
                info = tarfile.TarInfo(f"youtube-feed-reader-{app_version}/{relative.as_posix()}")
                info.size = len(data)
                info.mtime = epoch
                info.mode = 0o755 if relative == Path("install.sh") or relative.as_posix() in {"packaging/ytrss", "scripts/build_release.py", "scripts/install_linux.py"} else 0o644
                bundle.addfile(info, io.BytesIO(data))
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--work-dir", type=Path, default=None)
    parser.add_argument("--source-only", action="store_true")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    app_version = version()
    artifacts = [build_source(output, app_version)]
    if not args.source_only:
        if shutil.which("rpmbuild") is None:
            parser.error("rpmbuild is required; on Fedora install rpm-build and desktop-file-utils")
        work = args.work_dir.resolve() if args.work_dir else Path(tempfile.mkdtemp(prefix="ytrss-rpm-"))
        if any(character.isspace() for character in str(work)):
            parser.error("The RPM work directory must not contain whitespace; choose --work-dir /tmp/ytrss-rpm")
        for part in ["SOURCES", "SPECS", "BUILD", "BUILDROOT", "RPMS", "SRPMS", "TMP"]:
            (work / part).mkdir(parents=True, exist_ok=True)
        shutil.copy2(artifacts[0], work / "SOURCES" / f"youtube-feed-reader-{app_version}.tar.gz")
        spec = work / "SPECS/youtube-feed-reader.spec"
        spec.write_text((ROOT / "packaging/youtube-feed-reader.spec").read_text().replace("Version:        0.1.0", f"Version:        {app_version}"))
        subprocess.run(["rpmbuild", "-bb", "--define", f"_topdir {work}",
                        "--define", f"_tmppath {work / 'TMP'}", str(spec)], check=True)
        packages = sorted((work / "RPMS/noarch").glob(f"youtube-feed-reader-{app_version}-*.rpm"))
        if not packages:
            raise RuntimeError("The build did not produce an RPM")
        for package in packages:
            target = output / package.name
            shutil.copy2(package, target)
            artifacts.append(target)
    checksums = []
    for artifact in artifacts:
        with artifact.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        checksums.append(f"{checksum}  {artifact.name}\n")
        print(f"Built: {artifact} ({artifact.stat().st_size} bytes)")
    (output / "SHA256SUMS").write_text("".join(checksums))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

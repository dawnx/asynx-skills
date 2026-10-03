"""Build a skill-only release archive and its checksum manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "asx"
sys.path.insert(0, str(SKILL / "scripts"))

from asxlib.update_files import MAX_PACKAGE_BYTES, read_version, version_tuple


def build_release(source: Path, destination: Path) -> tuple[Path, Path]:
    version = read_version(source)
    if version is None:
        raise ValueError("Missing skill version")
    version_tuple(version)
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / f"asx-{version}.zip"
    manifest = destination / f"asx-{version}.json"
    files = [source / "SKILL.md"]
    # Only distributable code and instructions; never vendor, data, config or test artifacts.
    extensions = {
        "scripts": {".py"},
        "agents": {".yaml", ".yml"},
        "references": {".md"},
    }
    for folder, suffixes in extensions.items():
        files.extend(
            p
            for p in (source / folder).rglob("*")
            if p.is_file()
            and p.suffix in suffixes
            and not {"vendor", "__pycache__"}.intersection(p.relative_to(source).parts)
        )
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for path in sorted(files):
            if path.is_symlink():
                raise ValueError(f"Refusing symlink in release: {path}")
            name = "asx/" + path.relative_to(source).as_posix()
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            package.writestr(info, path.read_bytes())
        license_file = ROOT / "LICENSE"
        if license_file.is_file():
            info = zipfile.ZipInfo("asx/LICENSE", date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            package.writestr(info, license_file.read_bytes())
    if archive.stat().st_size > MAX_PACKAGE_BYTES:
        raise ValueError("Release exceeds the supported download size")
    manifest.write_text(
        json.dumps(
            {
                "schema": 1,
                "version": version,
                "python_min": "3.10",
                "archive": archive.name,
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return archive, manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="打包 Asynx Skill 稳定发布版")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    archive, manifest = build_release(SKILL, args.output_dir)
    print(
        json.dumps(
            {"archive": str(archive.resolve()), "manifest": str(manifest.resolve())}
        )
    )

#!/usr/bin/env python3
"""Verify an SDK snapshot against its reviewed manifest without changing files."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    args = parser.parse_args()
    root = args.snapshot.resolve()
    manifest = json.loads((root / "EXPORT_MANIFEST.json").read_text())
    records = manifest["files"]
    paths = [record["path"] for record in records]
    if len(paths) != len(set(paths)):
        raise SystemExit("Duplicate manifest paths")
    findings = []
    for record in records:
        relative = Path(record["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise SystemExit("Manifest path must stay inside snapshot")
        path = root / relative
        if path.is_symlink() or not path.is_file():
            findings.append({"path": str(relative), "reason": "Missing or nonregular file"})
            continue
        data = path.read_bytes()
        if len(data) != record["bytes"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
            findings.append({"path": str(relative), "reason": "Content differs from reviewed manifest"})
    expected = set(paths) | {"EXPORT_MANIFEST.json"}
    for path in root.rglob("*"):
        if path.is_file() or path.is_symlink():
            relative = path.relative_to(root).as_posix()
            if relative not in expected:
                findings.append({"path": relative, "reason": "Unreviewed file"})
    print(json.dumps({"passed": not findings, "files": len(paths), "findings": findings}, indent=2))
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())

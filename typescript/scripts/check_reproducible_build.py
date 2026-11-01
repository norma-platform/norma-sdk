# Modified for Norma.
"""Require repeated clean builds to emit identical public package file bytes."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build_payload() -> dict[str, str]:
    subprocess.run(["pnpm", "run", "build"], cwd=ROOT, check=True)
    files = sorted((ROOT / "dist").rglob("*"))
    if any(path.is_symlink() for path in files):
        raise SystemExit("Build output must not contain symbolic links")
    payload = {
        path.relative_to(ROOT / "dist").as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in files if path.is_file()
    }
    if not payload:
        raise SystemExit("Build produced no package files")
    return payload


def main() -> None:
    expected = build_payload()
    for _ in range(2):
        actual = build_payload()
        changed = sorted(name for name in expected.keys() | actual.keys()
                         if expected.get(name) != actual.get(name))
        if changed:
            raise SystemExit("Repeated clean builds changed package files: " + ", ".join(changed))
    print(f"Three clean builds emitted identical bytes for all {len(expected)} package files")


if __name__ == "__main__":
    main()

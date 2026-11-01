# Modified for Norma.
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "schema" / "instrumentation.schema.json"
DESTINATIONS = (
    ROOT / "python" / "src" / "norma" / "instrumentation.schema.json",
    ROOT / "python" / "src" / "_norma_config" / "instrumentation.schema.json",
    ROOT / "typescript" / "src" / "instrumentation.schema.json",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    source = SOURCE.read_bytes()
    stale = [path for path in DESTINATIONS if not path.exists() or path.read_bytes() != source]
    if args.check:
        if stale:
            print("Stale instrumentation schema copies: " + ", ".join(map(str, stale)))
            return 1
        return 0
    for path in stale:
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE, path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

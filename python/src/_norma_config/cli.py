"""Isolated CLI dispatch for offline instrumentation validation."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .coverage import check_rule_coverage
from .validation import validate_instrumentation_source


def _validate(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="norma config validate",
        description=(
            "Validate JSON/YAML instrumentation offline. Bindings and authorization "
            "are checked at startup."
        ),
    )
    parser.add_argument("source", type=Path, help="Configuration file or directory")
    parser.add_argument("--json", action="store_true", help="Write a machine-readable report")
    args = parser.parse_args(argv)
    report = validate_instrumentation_source(args.source)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    elif report["valid"]:
        print(
            f"Validated {report['files_checked']} configuration document(s) offline. "
            "Bindings and authorization remain startup checks."
        )
    else:
        for diagnostic in report["diagnostics"]:
            location = (
                f":{diagnostic['line']}:{diagnostic['column']}"
                if diagnostic["line"] is not None and diagnostic["column"] is not None
                else ""
            )
            pointer = f" {diagnostic['path']}" if diagnostic["path"] else ""
            print(
                f"{diagnostic['file']}{location}{pointer} [{diagnostic['code']}] "
                f"{diagnostic['message']}",
                file=sys.stderr,
            )
    return 0 if report["valid"] else 1


def _check(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="norma config check",
        description="Check local Rule selector coverage against offline configuration files.",
    )
    parser.add_argument("source", type=Path, help="Configuration file or directory")
    parser.add_argument("--rules", type=Path, required=True, help="Local Rule file or directory")
    parser.add_argument("--json", action="store_true", help="Write a machine-readable report")
    parser.add_argument(
        "--allow-unknown",
        action="store_true",
        help="Allow dependencies whose coverage cannot be proven statically",
    )
    args = parser.parse_args(argv)
    report, unknown_allowed = check_rule_coverage(
        args.source, args.rules, allow_unknown=args.allow_unknown
    )
    valid = bool(report["valid"])
    if args.json:
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    elif valid:
        print(
            f"Checked {report['files_checked']} configuration document(s) and "
            f"{report['rules_checked']} Rule(s) offline."
        )
        if unknown_allowed:
            print("Warning: unknown selector coverage was allowed; review the coverage report.")
        elif report["coverage_complete"]:
            print("All declared Rule selector paths are covered by the selected configurations.")
    else:
        for diagnostic in report["diagnostics"]:
            location = (
                f":{diagnostic['line']}:{diagnostic['column']}"
                if diagnostic["line"] is not None and diagnostic["column"] is not None
                else ""
            )
            pointer = f" {diagnostic['path']}" if diagnostic["path"] else ""
            print(
                f"{diagnostic['file']}{location}{pointer} [{diagnostic['code']}] "
                f"{diagnostic['message']}",
                file=sys.stderr,
            )
    return 0 if valid else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch validation without importing the SDK namespace or its settings."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) >= 2 and args[:2] == ["config", "validate"]:
        return _validate(args[2:])
    if len(args) >= 3 and args[:2] == ["config", "check"]:
        return _check(args[2:])
    from norma.cli import main as legacy_main

    return legacy_main(args)


if __name__ == "__main__":
    raise SystemExit(main())

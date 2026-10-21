# Modified for Norma.
"""Read-only diagnostics for hosted Norma connections."""

import argparse
import asyncio
import json
from collections.abc import Sequence
from importlib.metadata import version

import httpx

from norma.client import NormaClient
from norma.configuration import resolve_agent_key, resolve_url
from norma.models.server import GetAgentResponse
from norma.validation import ensure_agent_name


def _check_configuration() -> bool:
    """Validate without displaying credentials or endpoint contents."""
    valid = True
    for name, resolver in (
        ("NORMA_RUNTIME_URL", resolve_url),
        ("NORMA_AGENT_KEY", resolve_agent_key),
    ):
        try:
            resolver()
        except ValueError:
            print(f"{name}: missing or invalid")
            valid = False
        else:
            print(f"{name}: valid")
    return valid


async def _doctor(agent: str) -> int:
    """Read an authorized agent; never submit an check or create resources."""
    try:
        agent = ensure_agent_name(agent)
        async with NormaClient(timeout=10.0) as client:
            response = GetAgentResponse.model_validate(await client.check_connection(agent))
            if response.agent.agent_name != agent:
                raise ValueError("Unexpected agent response")
    except httpx.HTTPStatusError as exc:
        print(f"Agent access check failed (HTTP {exc.response.status_code}).")
        return 1
    except (httpx.RequestError, ValueError, TypeError, json.JSONDecodeError):
        print("Agent access check failed: invalid response, configuration, or connection.")
        return 1
    print("Agent access verified. Check compatibility and tenant routing are not verified.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run diagnostics, returning zero on success and one on a failed check.

    No command saves credentials, creates keys, or starts infrastructure.
    ``config check`` is offline; ``doctor`` performs one authenticated agent read.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    config = commands.add_parser("config", help="Validate environment configuration offline")
    config.add_subparsers(dest="config_command", required=True).add_parser("check")
    doctor = commands.add_parser("doctor", help="Check access to a registered hosted agent")
    doctor.add_argument(
        "--agent", required=True, help="Registered agent name authorized by the key"
    )
    commands.add_parser("version", help="Display the installed SDK version")
    args = parser.parse_args(argv)
    if args.command == "version":
        print(version("norma-sdk"))
        return 0
    if not _check_configuration():
        return 1
    if args.command == "doctor":
        return asyncio.run(_doctor(args.agent))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

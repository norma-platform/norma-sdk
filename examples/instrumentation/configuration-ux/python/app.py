"""Run one configured entrypoint against a deterministic public transport double."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, Literal

import httpx
import norma

RUNTIME_URL = "https://runtime.example.test/api/runtime"
CONTROL_CONTRACT = "rule-controls-v1"
EXAMPLE_DIRECTORY = Path(__file__).resolve().parent


async def respond(request: dict[str, str]) -> dict[str, str]:
    """Stand in for an existing application function; no model is called."""
    return {"message": f"Processed: {request['message']}"}


application = type("Application", (), {"respond": staticmethod(respond)})()


def local_transport() -> tuple[httpx.MockTransport, list[dict[str, Any]]]:
    """Return an in-process runtime double and the recorded finish request bodies."""
    finish_requests: list[dict[str, Any]] = []

    def acknowledged(payload: dict[str, Any]) -> httpx.Response:
        return httpx.Response(
            200,
            json={"control_contract": CONTROL_CONTRACT, **payload},
            headers={"X-Norma-Control-Contract": CONTROL_CONTRACT},
        )

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/agents/example-agent"):
            return httpx.Response(200, json={"agent": {"agent_name": "example-agent"}})
        if request.method == "GET" and path.endswith("/rules"):
            assert request.headers["X-Norma-Control-Contract"] == CONTROL_CONTRACT
            return acknowledged({"rules": []})
        if request.method == "POST" and path.endswith("/checks"):
            assert request.headers["X-Norma-Control-Contract"] == CONTROL_CONTRACT
            finish_requests.append(json.loads(request.content))
            return acknowledged(
                {
                    "execution_contract": "hosted-v1",
                    "verdict": "pass",
                    "reason": "Complete",
                    "reason_codes": [],
                    "held_by": [],
                    "would_hold_by": [],
                    "continuation": {"count": 0, "max": 2},
                    "allow_finish": True,
                    "outcome": "completed",
                }
            )
        return httpx.Response(404)

    return httpx.MockTransport(handle), finish_requests


async def main(config_format: Literal["json", "yaml"] = "json") -> None:
    config_path = EXAMPLE_DIRECTORY / "config" / f"example-agent.{config_format}"
    transport, finish_requests = local_transport()
    configured = await norma.aconfigure(
        config_path,
        bindings={"application": application},
        connection={
            "runtime_url": RUNTIME_URL,
            "agent_key": "local-demo-only",
            "transport": transport,
            "observability_enabled": False,
            "refresh_interval_seconds": 0,
        },
    )
    request = {
        "session_id": "session-demo",
        "turn_id": "turn-demo",
        "message": "show the new selectors",
    }
    try:
        result = await application.respond(request)
        turn = finish_requests[0]["step"]["input"]
        assert turn["session_id"] == request["session_id"]
        assert turn["turn_id"] == request["turn_id"]
        assert turn["user_request"] == request["message"]
        assert turn["final_message"] == result["message"]
        assert turn["platform"] == "norma-agent-runtime"
        assert turn["platform_version"] == "3.2"
        print(result["message"])
        print(
            f"Verified {config_format.upper()} configuration and canonical finish wire fields."
        )
    finally:
        await configured.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("json", "yaml"), default="json")
    asyncio.run(main(parser.parse_args().format))

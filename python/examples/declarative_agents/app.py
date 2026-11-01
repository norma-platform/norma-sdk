"""Two existing Norma identities sharing a tool, with request context and finish checks."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import norma


async def send_message(recipient: str, message: str) -> str:
    # Replace with the application's existing async message service.
    return f"sent to {recipient}: {message}"


tools = SimpleNamespace(send_message=send_message)


class Callbacks:
    def request_context(self, invocation: dict[str, Any]) -> dict[str, str]:
        request = invocation["args"][0]
        return {
            "request_id": request["turn_id"],
            "customer_id": request["customer_id"],
        }

    def record_verdict(self, payload: dict[str, Any]) -> None:
        verdict = payload["verdict"]
        print(f"{payload['agent_name']} finish verdict: {verdict.verdict}")


callbacks = Callbacks()


async def support_chat(request: dict[str, str]) -> str:
    return await tools.send_message("support-queue", request["message"])


async def billing_chat(request: dict[str, str]) -> str:
    return await tools.send_message("billing-queue", request["message"])


application = SimpleNamespace(
    support_chat=support_chat,
    billing_chat=billing_chat,
)


async def main() -> None:
    configured = await norma.aconfigure(
        "norma-agents",
        bindings={"application": application, "tools": tools, "callbacks": callbacks},
    )
    common = {
        "session_id": str(uuid.uuid4()),
        "customer_id": "customer-456",
    }
    try:
        print(
            await application.support_chat(
                {
                    **common,
                    "turn_id": str(uuid.uuid4()),
                    "message": "How can I help?",
                }
            )
        )
        print(
            await application.billing_chat(
                {
                    **common,
                    "turn_id": str(uuid.uuid4()),
                    "message": "I have a billing question.",
                }
            )
        )
    finally:
        receipts = await configured.aclose()
        for agent_name, receipt in receipts.items():
            print(agent_name, receipt.status)


if __name__ == "__main__":
    asyncio.run(main())

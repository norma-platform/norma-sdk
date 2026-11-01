"""Stream a local async generator and check its collected text at natural finish."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import norma
from norma.finish import FinishHeldError


async def stream_reply(request: dict[str, str]) -> Any:
    """Yield the reply in separate chunks to stand in for an application's stream."""
    yield "Hello, "
    yield request["message"]


application = SimpleNamespace(stream_reply=stream_reply)


async def main(*, connection: Mapping[str, object] | None = None) -> None:
    agent_name = os.environ["NORMA_AGENT_NAME"]
    if Path(agent_name).name != agent_name or not agent_name.strip():
        raise ValueError("NORMA_AGENT_NAME must be a single Agent identity")
    config_file = Path(__file__).resolve().parent / "config" / f"{agent_name}.json"
    configured = await norma.aconfigure(
        config_file,
        bindings={"application": application},
        connection=connection,
    )
    session_id = str(uuid.uuid4())
    request = {
        "session_id": session_id,
        "turn_id": str(uuid.uuid4()),
        "message": "your request",
    }
    try:
        try:
            async for chunk in application.stream_reply(request):
                print(chunk, end="", flush=True)
            print()
        except FinishHeldError as error:
            print(f"\nTurn held: {error.verdict.reason}")
            print(f"Collected text: {error.original_result}")
    finally:
        await configured.aclose()


if __name__ == "__main__":
    asyncio.run(main())

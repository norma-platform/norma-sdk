# Modified for Norma.
"""Check explicit agent access using the two hosted connection settings."""

import argparse
import asyncio
from norma import NormaClient, agents


async def main(agent_name: str) -> None:
    async with NormaClient() as client:
        result = await agents.get_agent(client, agent_name)
        print(result["agent"]["agent_name"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("agent_name")
    asyncio.run(main(parser.parse_args().agent_name))

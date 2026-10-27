"""One turn against the real ledger-api and Claude, for trying the agent end to end.

    LEDGER_ACCESS_TOKEN=... uv run python -m ledger_agent "I went to starbucks and bought coffee spending 5 dollars"

The token is a ledger-api access token (15 minutes) for the customer the transactions belong to. It's read from the
environment so it stays out of shell history. The Anthropic API key is read from SSM with your AWS credentials.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime

from anthropic import AsyncAnthropic

from .agent import ChatMessage, Claude, run_turn
from .clock import today_in
from .config import LEDGER_MCP_URL, anthropic_api_key
from .ledger import Unauthorized
from .mcp_ledger import connect


async def main(message: str, time_zone: str) -> int:
    token = os.environ.get("LEDGER_ACCESS_TOKEN")
    if not token:
        print("Set LEDGER_ACCESS_TOKEN to a ledger-api access token.", file=sys.stderr)
        return 2
    messages: list[ChatMessage] = [{"role": "user", "content": message}]
    async with AsyncAnthropic(api_key=anthropic_api_key()) as claude, connect(LEDGER_MCP_URL, token) as ledger:
        try:
            result = await run_turn(
                model=Claude(claude),
                ledger=ledger,
                messages=messages,
                today=today_in(time_zone, datetime.now(UTC)),
            )
        except Unauthorized:
            print("ledger-api rejected the token; it may have expired.", file=sys.stderr)
            return 1
    print(result.reply)
    for transaction in result.transactions:
        print(json.dumps(transaction, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="python -m ledger_agent")
    parser.add_argument("message")
    parser.add_argument("--time-zone", default="Australia/Sydney")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.message, args.time_zone)))

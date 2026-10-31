"""Lambda entry point: wires real dependencies into the handler once per cold start."""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from anthropic import AsyncAnthropic

from .agent import Claude
from .config import LEDGER_MCP_URL, SMOKE_TEST_USER_ID, anthropic_api_key, usage_table
from .handler import Deps, create_handler
from .mcp_ledger import connect
from .usage import count_message

logging.getLogger("ledger_agent").setLevel(logging.INFO)

# One loop for the life of the container: the Anthropic client's connection pool is bound to the loop that
# created it, so a fresh asyncio.run() per invocation would leave it on a closed loop.
_loop = asyncio.new_event_loop()
_usage = usage_table()
_handle = create_handler(
    Deps(
        model=Claude(AsyncAnthropic(api_key=anthropic_api_key())),
        connect=lambda token: connect(LEDGER_MCP_URL, token),
        now=lambda: datetime.now(UTC),
        count_message=lambda user_id, now: count_message(_usage, user_id, now, exempt=SMOKE_TEST_USER_ID),
    )
)


def handler(event: dict[str, Any], _context: object) -> dict[str, Any]:
    return _loop.run_until_complete(_handle(event))

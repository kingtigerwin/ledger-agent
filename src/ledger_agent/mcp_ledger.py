"""The Ledger interface over ledger-api's MCP endpoint, as the customer whose token it holds."""

from collections.abc import AsyncGenerator, Awaitable
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from .ledger import LedgerUnavailable, ToolOutcome, ToolSpec, Unauthorized

# Well inside the API Gateway's 30-second limit on the whole turn.
TIMEOUT_SECONDS = 10.0


class McpLedger:
    def __init__(self, session: ClientSession, statuses: list[int]):
        self._session = session
        self._statuses = statuses
        self._initialized = False

    async def list_tools(self) -> list[ToolSpec]:
        if not self._initialized:
            await self._guard(self._session.initialize())
            self._initialized = True
        listed = await self._guard(self._session.list_tools())
        return [ToolSpec(tool.name, tool.description or "", tool.input_schema) for tool in listed.tools]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        result = await self._guard(self._session.call_tool(name, arguments))
        text = "".join(block.text for block in result.content if isinstance(block, types.TextContent))
        return ToolOutcome(text, is_error=bool(result.is_error))

    async def _guard[T](self, call: Awaitable[T]) -> T:
        """The MCP SDK reports every HTTP error the same way, so the status the hook saw tells them apart."""
        try:
            return await call
        except Exception as err:
            if self._statuses and self._statuses[-1] in (401, 403):
                raise Unauthorized("ledger-api rejected the access token") from err
            raise LedgerUnavailable(str(err) or type(err).__name__) from err


@asynccontextmanager
async def connect(
    url: str, token: str, *, transport: httpx2.AsyncBaseTransport | None = None
) -> AsyncGenerator[McpLedger]:
    """A ledger session for one turn. `transport` is for tests; production uses the network."""
    statuses: list[int] = []

    async def note_status(response: httpx2.Response) -> None:
        statuses.append(response.status_code)

    client = httpx2.AsyncClient(
        headers={"authorization": f"Bearer {token}"},
        timeout=TIMEOUT_SECONDS,
        event_hooks={"response": [note_status]},
        transport=transport,
    )
    try:
        # ledger-api is stateless, so there's no session to end with a DELETE.
        async with (
            client,
            streamable_http_client(url, http_client=client, terminate_on_close=False) as (read, write),
            ClientSession(read, write) as session,
        ):
            yield McpLedger(session, statuses)
    except BaseExceptionGroup as group:
        # The transport's task group wraps whatever leaves the session, including the caller's own errors;
        # callers catch Unauthorized, ModelUnavailable and the rest by type, so hand back the one that was raised.
        raise _sole_error(group) from None


def _sole_error(group: BaseExceptionGroup[BaseException]) -> BaseException:
    """The one error inside (possibly nested) single-error groups; a group of several stays as it is."""
    error: BaseException = group
    while len(members := _members(error)) == 1:
        error = members[0]
    return error


def _members(error: BaseException) -> tuple[BaseException, ...]:
    # Checked on the type, not with isinstance: pyright can't infer the members of a group narrowed from BaseException.
    return getattr(error, "exceptions", ()) if issubclass(type(error), BaseExceptionGroup) else ()

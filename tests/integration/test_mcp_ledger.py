"""McpLedger against a real MCP server (the SDK's own), in process, behind a bearer-token check like ledger-api's."""

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx2
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

from ledger_agent.ledger import LedgerUnavailable, Unauthorized
from ledger_agent.mcp_ledger import McpLedger, connect

URL = "http://127.0.0.1/api/mcp"
GOOD_TOKEN = "customer-token"


def ledger_server() -> MCPServer:
    server = MCPServer("fake-ledger-api")

    @server.tool(description="Records one expense.")
    def create_transaction(date: str, item: str, amountCents: int) -> str:
        if amountCents <= 0:
            # ledger-api answers rule violations as tool errors carrying its message.
            raise ToolError("amountCents must be positive")
        return json.dumps({"id": "01JBXCOFFEE", "date": date, "item": item, "amountCents": amountCents})

    @server.tool(description="Lists transactions.")
    def list_transactions() -> str:
        return json.dumps({"items": [], "nextCursor": None})

    return server


def behind_authorizer(app: ASGIApp, *, status_for_good_token: int | None) -> ASGIApp:
    """Like ledger-api's API Gateway authorizer: 401 unless the bearer token is the customer's."""

    async def guarded(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            authorization = dict(scope["headers"]).get(b"authorization")
            status = 401 if authorization != f"Bearer {GOOD_TOKEN}".encode() else status_for_good_token
            if status is not None:
                await send({"type": "http.response.start", "status": status, "headers": []})
                await send({"type": "http.response.body", "body": b'{"message":"Unauthorized"}'})
                return
        await app(scope, receive, send)

    return guarded


@asynccontextmanager
async def served(token: str = GOOD_TOKEN, *, status_for_good_token: int | None = None) -> AsyncGenerator[McpLedger]:
    """A ledger connected to the fake server. Not a fixture: the server's lifespan must exit in the task it entered."""
    app = ledger_server().streamable_http_app(
        streamable_http_path="/api/mcp",
        json_response=True,
        stateless_http=True,
        # The in-memory transport's Host isn't a real one; rebinding protection isn't what's under test.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    transport = httpx2.ASGITransport(app=behind_authorizer(app, status_for_good_token=status_for_good_token))
    async with app.router.lifespan_context(app), connect(URL, token, transport=transport) as ledger:
        yield ledger


class TestGivenTheCustomersToken:
    async def test_it_lists_the_tools_with_their_descriptions_and_schemas(self) -> None:
        async with served() as ledger:
            tools = {tool.name: tool for tool in await ledger.list_tools()}
        assert set(tools) == {"create_transaction", "list_transactions"}
        create = tools["create_transaction"]
        assert create.description == "Records one expense."
        assert set(create.input_schema["properties"]) == {"date", "item", "amountCents"}

    async def test_a_tool_call_returns_the_servers_json_text(self) -> None:
        async with served() as ledger:
            await ledger.list_tools()
            outcome = await ledger.call_tool(
                "create_transaction", {"date": "2026-10-27", "item": "coffee", "amountCents": 500}
            )
        assert outcome.is_error is False
        assert json.loads(outcome.text)["id"] == "01JBXCOFFEE"

    async def test_a_tool_the_server_rejects_comes_back_as_a_tool_error_with_its_message(self) -> None:
        async with served() as ledger:
            await ledger.list_tools()
            outcome = await ledger.call_tool(
                "create_transaction", {"date": "2026-10-27", "item": "coffee", "amountCents": -5}
            )
        assert outcome.is_error is True
        assert "amountCents must be positive" in outcome.text


class TestGivenATokenTheAuthorizerRejects:
    async def test_listing_tools_raises_unauthorized(self) -> None:
        async with served("expired") as ledger:
            with pytest.raises(Unauthorized):
                await ledger.list_tools()


class TestGivenTheServerFails:
    async def test_listing_tools_raises_ledger_unavailable(self) -> None:
        async with served(status_for_good_token=502) as ledger:
            with pytest.raises(LedgerUnavailable):
                await ledger.list_tools()

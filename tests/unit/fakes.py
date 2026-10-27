"""Stand-ins for Claude and ledger-api's MCP server, so the agent loop runs without either."""

import json
from collections.abc import Mapping
from typing import Any

from anthropic.types import Message

from ledger_agent.ledger import ToolOutcome, ToolSpec, Unauthorized

# The six tools ledger-api's MCP server lists for a customer token (see ledger-api/src/mcp/handler.ts).
LEDGER_TOOLS = [
    ToolSpec(name, f"{name} description", {"type": "object", "properties": {"probe": {"type": "string"}}})
    for name in (
        "create_transaction",
        "get_transaction",
        "list_transactions",
        "get_monthly_summary",
        "update_transaction",
        "delete_transaction",
    )
]


class FakeLedger:
    def __init__(self, results: Mapping[str, ToolOutcome | Exception] | None = None, *, authorized: bool = True):
        self.results = dict(results or {})
        self.authorized = authorized
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self) -> list[ToolSpec]:
        if not self.authorized:
            raise Unauthorized("token rejected")
        return LEDGER_TOOLS

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        self.calls.append((name, arguments))
        result = self.results.get(name, ToolOutcome("{}", is_error=False))
        if isinstance(result, Exception):
            raise result
        return result


class FakeModel:
    """Answers each create() with the next scripted message and records what was sent."""

    def __init__(self, *responses: Message):
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    async def create(self, **params: Any) -> Message:
        # Copy the list: the agent keeps appending to its own, which would rewrite what this request sent.
        self.requests.append({**params, "messages": list(params["messages"])})
        if not self.responses:
            raise AssertionError("the agent called Claude more times than the test scripted")
        return self.responses.pop(0)


def message(*content: dict[str, Any], stop_reason: str = "end_turn") -> Message:
    return Message.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-5-5",
            "content": list(content),
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    )


def text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


def thinking() -> dict[str, Any]:
    return {"type": "thinking", "thinking": "", "signature": "sig"}


def tool_use(tool_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": arguments}


def stored(**fields: Any) -> ToolOutcome:
    return ToolOutcome(json.dumps(fields), is_error=False)

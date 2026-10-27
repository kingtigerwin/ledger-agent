"""What the agent needs from ledger-api's MCP server, independent of the MCP SDK."""

from dataclasses import dataclass
from typing import Any, Protocol


class Unauthorized(Exception):
    """ledger-api rejected the customer's access token."""


class LedgerUnavailable(Exception):
    """ledger-api couldn't be reached or failed, so a write may or may not have happened."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolOutcome:
    """A tool's JSON text result, or ledger-api's error message when `is_error`."""

    text: str
    is_error: bool


class Ledger(Protocol):
    async def list_tools(self) -> list[ToolSpec]:
        """The customer's tools. Raises Unauthorized if ledger-api rejects the token."""
        ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        """Calls one tool, once. Raises LedgerUnavailable on transport or server failure."""
        ...

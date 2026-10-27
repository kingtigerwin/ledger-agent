"""One chat turn: Claude reads the customer's message and records or looks up transactions over MCP."""

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal, Protocol, TypedDict

from anthropic.types import (
    AssistantMessageParam,
    Message,
    MessageParam,
    ToolParam,
    ToolResultBlockParam,
    ToolUseBlock,
    UserMessageParam,
)

from .ledger import Ledger, LedgerUnavailable, ToolSpec

MODEL_ID = "claude-haiku-5-5"
# Room for adaptive thinking plus a one-sentence reply and a few tool calls.
MAX_TOKENS = 2048
# Claude calls per turn; recording several expenses usually takes two.
MAX_ROUNDS = 4

# The only ledger-api tools Claude sees, in a fixed order. Changing or deleting transactions happens in the UI.
OFFERED_TOOLS = ("create_transaction", "list_transactions", "get_monthly_summary")
CREATE = "create_transaction"

DECLINED = "Sorry, I can't help with that."
UNFINISHED = "Sorry, I couldn't finish that. Please check your transactions before trying again."
UNREACHABLE = (
    "ledger-api couldn't be reached, so this may or may not have been saved. "
    "Don't call create_transaction again for it; tell the customer to check their transactions."
)

SYSTEM_PROMPT = """\
You record a customer's spending in their ledger and answer questions about it.

When the customer describes something they bought, record it straight away with create_transaction, one call per \
expense. Then confirm what you saved in one short sentence: amount, merchant, item, category and date.
- Amounts are Australian dollars, the only currency the ledger accepts; "dollars" and "$" mean AUD. If the customer \
names another currency, say only AUD is supported instead of converting.
- If the amount or what was bought is missing, ask for it instead of recording; never guess an amount.
- Use the date the customer gives ("yesterday", "last Friday"), otherwise today.
- Pick the closest category, or other. Write merchant names in their usual form ("Starbucks", "Woolworths").
- If a tool call fails, don't repeat it; tell the customer what happened.

For questions about past spending, use list_transactions or get_monthly_summary. You can't edit or delete \
transactions; the customer does that in the app.

Today is {weekday} {today} in the customer's time zone."""


class ChatMessage(TypedDict):
    role: Literal["user", "assistant"]
    content: str


class Model(Protocol):
    async def create(self, **params: Any) -> Message: ...


@dataclass
class TurnResult:
    reply: str
    transactions: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])


async def run_turn(
    *,
    model: Model,
    ledger: Ledger,
    messages: list[ChatMessage],
    today: date,
    max_rounds: int = MAX_ROUNDS,
) -> TurnResult:
    # Checks the token before any Claude call: an Unauthorized here costs nothing.
    tools = _offered(await ledger.list_tools())
    offered = {tool["name"] for tool in tools}
    system = SYSTEM_PROMPT.format(weekday=today.strftime("%A"), today=today.isoformat())
    history: list[MessageParam] = [_message_param(m) for m in messages]
    result = TurnResult(reply="")

    for _ in range(max_rounds):
        response = await model.create(
            model=MODEL_ID,
            max_tokens=MAX_TOKENS,
            system=system,
            tools=tools,
            messages=history,
            output_config={"effort": "low"},
        )
        if response.stop_reason == "refusal":
            result.reply = DECLINED
            return result
        if response.stop_reason != "tool_use":
            # max_tokens and anything unexpected leave a half-written reply; don't show it.
            finished = response.stop_reason in ("end_turn", "stop_sequence")
            result.reply = (_text(response) if finished else "") or UNFINISHED
            return result

        # Claude's turn goes back unchanged, thinking blocks included, followed by every result in one message.
        history.append(AssistantMessageParam(role="assistant", content=response.content))
        results = [
            await _call(ledger, block, offered, result) for block in response.content if isinstance(block, ToolUseBlock)
        ]
        history.append(UserMessageParam(role="user", content=results))

    result.reply = UNFINISHED
    return result


def _message_param(message: ChatMessage) -> MessageParam:
    if message["role"] == "user":
        return UserMessageParam(role="user", content=message["content"])
    return AssistantMessageParam(role="assistant", content=message["content"])


def _offered(specs: list[ToolSpec]) -> list[ToolParam]:
    by_name = {spec.name: spec for spec in specs}
    return [
        {"name": name, "description": by_name[name].description, "input_schema": by_name[name].input_schema}
        for name in OFFERED_TOOLS
        if name in by_name
    ]


async def _call(ledger: Ledger, block: ToolUseBlock, offered: set[str], result: TurnResult) -> ToolResultBlockParam:
    """Runs one tool call exactly once (create_transaction isn't idempotent) and records what it created."""
    if block.name not in offered:
        return _tool_result(block, f"{block.name} isn't available to you.", is_error=True)
    try:
        outcome = await ledger.call_tool(block.name, dict(block.input))
    except LedgerUnavailable:
        return _tool_result(block, UNREACHABLE, is_error=True)
    if block.name == CREATE and not outcome.is_error:
        result.transactions.append(json.loads(outcome.text))
    return _tool_result(block, outcome.text, is_error=outcome.is_error)


def _tool_result(block: ToolUseBlock, content: str, *, is_error: bool) -> ToolResultBlockParam:
    return {"type": "tool_result", "tool_use_id": block.id, "content": content, "is_error": is_error}


def _text(response: Message) -> str:
    return "".join(block.text for block in response.content if block.type == "text").strip()

"""One chat turn: Claude reads the customer's message and records or looks up transactions over MCP."""

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal, Protocol, TypedDict

from anthropic import APIError, AsyncAnthropic
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
# ledger-api lists list_all_transactions only for admin tokens, so customers never get it.
OFFERED_TOOLS = ("create_transaction", "list_transactions", "get_monthly_summary", "list_all_transactions")
CREATE = "create_transaction"
LIST = "list_transactions"
SUMMARY = "get_monthly_summary"
LIST_ALL = "list_all_transactions"

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

For questions about past spending, use list_transactions or get_monthly_summary. The app shows the customer every \
transaction and summary these return, as a table, so don't list them in your reply: answer in one or two sentences \
(how many, the total, anything notable). To list a whole period, pass from and to with limit 100, and follow \
nextCursor until it's null. You can't edit or delete transactions; the customer does that in the app.
{admin}
Today is {weekday} {today} in the customer's time zone."""

# Added only when ledger-api offers list_all_transactions, i.e. the customer is an admin.
ADMIN_PROMPT = """
This customer is an admin. For anyone else's spending, or everyone's (their own included), use \
list_all_transactions: pass username for one person, or leave it out for everyone; each item says whose it is. \
The app shows these as a table too, so answer briefly (per-person totals are useful). Expenses the customer \
describes are still recorded only in their own ledger, never for someone else.
"""


class ChatMessage(TypedDict):
    role: Literal["user", "assistant"]
    content: str


class ModelUnavailable(Exception):
    """Claude couldn't be called (after the SDK's own retries)."""


class Model(Protocol):
    async def create(self, **params: Any) -> Message:
        """Raises ModelUnavailable when Claude can't be reached or rejects the request."""
        ...


class Claude:
    """The real Model: non-streaming Messages API calls through the Anthropic SDK."""

    def __init__(self, client: AsyncAnthropic):
        self._client = client

    async def create(self, **params: Any) -> Message:
        try:
            return await self._client.messages.create(stream=False, **params)
        except APIError as err:
            raise ModelUnavailable(type(err).__name__) from err


@dataclass
class TurnResult:
    reply: str
    # What create_transaction recorded this turn.
    transactions: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    # What the lookups returned, for the UI to show as data: list_transactions items (each once) and
    # get_monthly_summary results.
    listed: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    summaries: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])


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
    system = SYSTEM_PROMPT.format(
        weekday=today.strftime("%A"), today=today.isoformat(), admin=ADMIN_PROMPT if LIST_ALL in offered else ""
    )
    history: list[MessageParam] = [_message_param(m) for m in messages]
    result = TurnResult(reply="")

    for _ in range(max_rounds):
        try:
            response = await model.create(
                model=MODEL_ID,
                max_tokens=MAX_TOKENS,
                system=system,
                tools=tools,
                messages=history,
                output_config={"effort": "low"},
            )
        except ModelUnavailable:
            # Once something is saved, the customer must hear about it, so report it rather than fail the turn.
            if not result.transactions:
                raise
            result.reply = UNFINISHED
            return result
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
    """Runs one tool call exactly once (create_transaction isn't idempotent) and keeps what it returned."""
    if block.name not in offered:
        return _tool_result(block, f"{block.name} isn't available to you.", is_error=True)
    try:
        outcome = await ledger.call_tool(block.name, dict(block.input))
    except LedgerUnavailable:
        return _tool_result(block, UNREACHABLE, is_error=True)
    if not outcome.is_error:
        _keep(block.name, json.loads(outcome.text), result)
    return _tool_result(block, outcome.text, is_error=outcome.is_error)


def _keep(tool: str, payload: dict[str, Any], result: TurnResult) -> None:
    if tool == CREATE:
        result.transactions.append(payload)
    elif tool == SUMMARY:
        result.summaries.append(payload)
    elif tool in (LIST, LIST_ALL):
        # Claude may list overlapping ranges or re-read a page; the UI should see each transaction once.
        seen = {t["id"] for t in result.listed}
        items: list[dict[str, Any]] = payload.get("items", [])
        result.listed.extend(item for item in items if item["id"] not in seen)


def _tool_result(block: ToolUseBlock, content: str, *, is_error: bool) -> ToolResultBlockParam:
    return {"type": "tool_result", "tool_use_id": block.id, "content": content, "is_error": is_error}


def _text(response: Message) -> str:
    return "".join(block.text for block in response.content if block.type == "text").strip()

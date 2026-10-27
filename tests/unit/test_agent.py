from datetime import date
from typing import Any

import pytest

from ledger_agent.agent import run_turn
from ledger_agent.ledger import LedgerUnavailable, ToolOutcome, Unauthorized

from .fakes import FakeLedger, FakeModel, message, stored, text, thinking, tool_use

TODAY = date(2026, 10, 27)
STARBUCKS = "I went to starbucks and bought coffee spending 5 dollars"
COFFEE = {"date": "2026-10-27", "merchant": "Starbucks", "category": "dining", "item": "coffee", "amountCents": 500}
COFFEE_STORED = {**COFFEE, "id": "01JBXCOFFEE", "currency": "AUD", "version": 1}


async def turn(model: FakeModel, ledger: FakeLedger, content: str = STARBUCKS, **options: Any):
    return await run_turn(
        model=model, ledger=ledger, messages=[{"role": "user", "content": content}], today=TODAY, **options
    )


def tool_results(request: dict[str, Any]) -> list[dict[str, Any]]:
    """The tool_result blocks in the last (user) message of a request."""
    return list(request["messages"][-1]["content"])


class TestGivenATokenTheLedgerRejects:
    async def test_it_raises_unauthorized_without_calling_claude(self) -> None:
        model = FakeModel()
        with pytest.raises(Unauthorized):
            await turn(model, FakeLedger(authorized=False))
        assert model.requests == []


class TestGivenAMessageDescribingAnExpense:
    @pytest.fixture
    async def outcome(self):
        model = FakeModel(
            message(thinking(), tool_use("t1", "create_transaction", COFFEE), stop_reason="tool_use"),
            message(text("Recorded $5.00 at Starbucks for coffee (dining) today.")),
        )
        ledger = FakeLedger({"create_transaction": stored(**COFFEE_STORED)})
        result = await turn(model, ledger)
        return model, ledger, result

    async def test_it_records_the_transaction_over_mcp(self, outcome: Any) -> None:
        _, ledger, _ = outcome
        assert ledger.calls == [("create_transaction", COFFEE)]

    async def test_it_returns_the_reply_and_the_stored_transaction(self, outcome: Any) -> None:
        _, _, result = outcome
        assert result.reply == "Recorded $5.00 at Starbucks for coffee (dining) today."
        assert result.transactions == [COFFEE_STORED]

    async def test_it_sends_the_ledger_result_back_to_claude(self, outcome: Any) -> None:
        model, _, _ = outcome
        [result] = tool_results(model.requests[1])
        assert result["type"] == "tool_result"
        assert result["tool_use_id"] == "t1"
        assert result["is_error"] is False
        assert "01JBXCOFFEE" in result["content"]

    async def test_it_passes_claudes_turn_back_unchanged_including_thinking(self, outcome: Any) -> None:
        model, _, _ = outcome
        assistant = model.requests[1]["messages"][1]
        assert assistant["role"] == "assistant"
        assert [block.type for block in assistant["content"]] == ["thinking", "tool_use"]


class TestWhatClaudeIsGiven:
    @pytest.fixture
    async def request_sent(self) -> dict[str, Any]:
        model = FakeModel(message(text("How much was it?")))
        await turn(model, FakeLedger(), "I bought coffee")
        return model.requests[0]

    async def test_only_create_list_and_summary_are_offered(self, request_sent: dict[str, Any]) -> None:
        assert [t["name"] for t in request_sent["tools"]] == [
            "create_transaction",
            "list_transactions",
            "get_monthly_summary",
        ]

    async def test_tools_keep_the_ledgers_descriptions_and_schemas(self, request_sent: dict[str, Any]) -> None:
        create = request_sent["tools"][0]
        assert create["description"] == "create_transaction description"
        assert create["input_schema"] == {"type": "object", "properties": {"probe": {"type": "string"}}}

    async def test_the_system_prompt_has_the_customers_date(self, request_sent: dict[str, Any]) -> None:
        assert "2026-10-27" in request_sent["system"]
        assert "Tuesday" in request_sent["system"]

    async def test_it_uses_haiku_at_low_effort_without_sampling_parameters(self, request_sent: dict[str, Any]) -> None:
        assert request_sent["model"] == "claude-haiku-5-5"
        assert request_sent["output_config"] == {"effort": "low"}
        assert not {"temperature", "top_p", "top_k"} & request_sent.keys()

    async def test_the_conversation_is_sent_as_is(self, request_sent: dict[str, Any]) -> None:
        assert request_sent["messages"] == [{"role": "user", "content": "I bought coffee"}]


class TestGivenTheLedgerRejectsTheTransaction:
    async def test_claude_gets_a_tool_error_and_nothing_is_reported_as_recorded(self) -> None:
        model = FakeModel(
            message(tool_use("t1", "create_transaction", COFFEE), stop_reason="tool_use"),
            message(text("Sorry, that couldn't be saved.")),
        )
        ledger = FakeLedger({"create_transaction": ToolOutcome("amountCents must be positive", is_error=True)})
        result = await turn(model, ledger)
        [sent] = tool_results(model.requests[1])
        assert sent["is_error"] is True
        assert sent["content"] == "amountCents must be positive"
        assert result.transactions == []


class TestGivenTheLedgerCannotBeReached:
    async def test_the_call_is_not_retried_and_claude_is_told_it_may_not_be_saved(self) -> None:
        model = FakeModel(
            message(tool_use("t1", "create_transaction", COFFEE), stop_reason="tool_use"),
            message(text("I couldn't confirm that was saved.")),
        )
        ledger = FakeLedger({"create_transaction": LedgerUnavailable("timed out")})
        result = await turn(model, ledger)
        assert len(ledger.calls) == 1
        [sent] = tool_results(model.requests[1])
        assert sent["is_error"] is True
        assert "may or may not have been saved" in sent["content"]
        assert result.transactions == []


class TestGivenClaudeCallsAToolItWasNotOffered:
    async def test_the_call_is_refused_without_reaching_the_ledger(self) -> None:
        model = FakeModel(
            message(tool_use("t1", "delete_transaction", {"id": "01JBX"}), stop_reason="tool_use"),
            message(text("I can't delete transactions.")),
        )
        ledger = FakeLedger()
        await turn(model, ledger)
        assert ledger.calls == []
        [sent] = tool_results(model.requests[1])
        assert sent["is_error"] is True


class TestGivenSeveralExpensesInOneMessage:
    async def test_every_tool_result_goes_back_in_one_message_and_all_are_returned(self) -> None:
        lunch = {**COFFEE, "item": "lunch", "amountCents": 1500}
        model = FakeModel(
            message(
                tool_use("t1", "create_transaction", COFFEE),
                tool_use("t2", "create_transaction", lunch),
                stop_reason="tool_use",
            ),
            message(text("Recorded both.")),
        )
        ledger = FakeLedger({"create_transaction": stored(**COFFEE_STORED)})
        result = await turn(model, ledger, "coffee $5 and lunch $15 at starbucks")
        assert [r["tool_use_id"] for r in tool_results(model.requests[1])] == ["t1", "t2"]
        assert len(result.transactions) == 2


class TestGivenClaudeKeepsCallingTools:
    async def test_it_stops_after_the_round_cap_and_still_returns_what_was_recorded(self) -> None:
        keep_going = [
            message(tool_use(f"t{i}", "create_transaction", COFFEE), stop_reason="tool_use") for i in range(3)
        ]
        model = FakeModel(*keep_going)
        ledger = FakeLedger({"create_transaction": stored(**COFFEE_STORED)})
        result = await turn(model, ledger, max_rounds=3)
        assert len(model.requests) == 3
        assert len(result.transactions) == 3
        assert result.reply != ""


class TestGivenClaudeDeclines:
    async def test_it_replies_with_a_short_apology(self) -> None:
        model = FakeModel(message(stop_reason="refusal"))
        result = await turn(model, FakeLedger())
        assert result.reply.startswith("Sorry")
        assert result.transactions == []


class TestGivenClaudeRunsOutOfTokens:
    async def test_it_replies_with_a_short_apology_rather_than_nothing(self) -> None:
        model = FakeModel(message(thinking(), stop_reason="max_tokens"))
        result = await turn(model, FakeLedger())
        assert result.reply.startswith("Sorry")

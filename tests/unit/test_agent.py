from datetime import date
from typing import Any

import pytest

from ledger_agent.agent import ModelUnavailable, run_turn
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

    async def test_customers_get_no_admin_instructions(self, request_sent: dict[str, Any]) -> None:
        assert "list_all_transactions" not in request_sent["system"]

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


class TestGivenClaudeFailsAfterSomethingWasRecorded:
    async def test_it_still_returns_what_was_recorded(self) -> None:
        model = FakeModel(
            message(tool_use("t1", "create_transaction", COFFEE), stop_reason="tool_use"),
            ModelUnavailable("overloaded"),
        )
        ledger = FakeLedger({"create_transaction": stored(**COFFEE_STORED)})
        result = await turn(model, ledger)
        assert result.reply.startswith("Sorry")
        assert result.transactions == [COFFEE_STORED]


class TestGivenClaudeFailsBeforeAnythingWasRecorded:
    async def test_the_failure_is_raised(self) -> None:
        model = FakeModel(ModelUnavailable("overloaded"))
        with pytest.raises(ModelUnavailable):
            await turn(model, FakeLedger())


OCTOBER = {"from": "2026-10-01", "to": "2026-10-31", "limit": 100}
LUNCH_STORED = {**COFFEE_STORED, "id": "01JBXLUNCH", "item": "lunch", "amountCents": 1500}
GROCERIES_STORED = {**COFFEE_STORED, "id": "01JBXGROCERIES", "category": "groceries", "amountCents": 8240}


def page(*items: dict[str, Any], next_cursor: str | None = None) -> ToolOutcome:
    return stored(items=list(items), nextCursor=next_cursor)


class TestGivenTheCustomerAsksToSeeTheirExpenses:
    @pytest.fixture
    async def result(self) -> Any:
        model = FakeModel(
            message(tool_use("t1", "list_transactions", OCTOBER), stop_reason="tool_use"),
            message(text("Here are your 2 expenses for October, $20.00 in total.")),
        )
        ledger = FakeLedger({"list_transactions": page(COFFEE_STORED, LUNCH_STORED)})
        return await turn(model, ledger, "I want to see all my expenses for this month")

    async def test_the_listed_transactions_come_back_as_data(self, result: Any) -> None:
        assert result.listed == [COFFEE_STORED, LUNCH_STORED]

    async def test_nothing_is_reported_as_recorded(self, result: Any) -> None:
        assert result.transactions == []
        assert result.summaries == []


class TestGivenClaudeFollowsTheCursorOrListsTheSameTransactionTwice:
    async def test_every_page_is_kept_once_in_order(self) -> None:
        model = FakeModel(
            message(tool_use("t1", "list_transactions", OCTOBER), stop_reason="tool_use"),
            message(tool_use("t2", "list_transactions", {**OCTOBER, "cursor": "c2"}), stop_reason="tool_use"),
            message(text("Here are your 3 expenses for October.")),
        )

        class PagedLedger(FakeLedger):
            async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
                self.calls.append((name, arguments))
                if "cursor" in arguments:
                    return page(LUNCH_STORED, GROCERIES_STORED)
                return page(COFFEE_STORED, LUNCH_STORED, next_cursor="c2")

        result = await turn(model, PagedLedger(), "show all my expenses this month")
        assert [t["id"] for t in result.listed] == ["01JBXCOFFEE", "01JBXLUNCH", "01JBXGROCERIES"]


class TestGivenTheCustomerAsksHowMuchTheySpent:
    async def test_the_summary_comes_back_as_data(self) -> None:
        summary = {
            "month": "2026-10",
            "currency": "AUD",
            "totalCents": 2000,
            "byCategory": [{"category": "dining", "totalCents": 2000}],
        }
        model = FakeModel(
            message(tool_use("t1", "get_monthly_summary", {"month": "2026-10"}), stop_reason="tool_use"),
            message(text("You've spent $20.00 so far this October, all on dining.")),
        )
        ledger = FakeLedger({"get_monthly_summary": stored(**summary)})
        result = await turn(model, ledger, "how much have I spent this month?")
        assert result.summaries == [summary]
        assert result.listed == []


class TestGivenALookupTheLedgerRejects:
    async def test_nothing_is_reported_as_listed(self) -> None:
        model = FakeModel(
            message(tool_use("t1", "list_transactions", {"cursor": "bad"}), stop_reason="tool_use"),
            message(text("Sorry, I couldn't load your expenses.")),
        )
        ledger = FakeLedger({"list_transactions": ToolOutcome("invalid cursor", is_error=True)})
        result = await turn(model, ledger, "show my expenses")
        assert result.listed == []


class TestThePrompt:
    async def test_it_tells_claude_the_app_shows_lookups_so_the_reply_stays_short(self) -> None:
        model = FakeModel(message(text("Hi!")))
        await turn(model, FakeLedger(), "hi")
        system = model.requests[0]["system"]
        assert "limit 100" in system
        assert "don't list them" in system


HAORAN_COFFEE = {**COFFEE_STORED, "id": "01JBXHAORAN", "userId": "u-haoran", "username": "haoran"}
CHACE_LUNCH = {**LUNCH_STORED, "id": "01JBXCHACE", "userId": "u-chace", "username": "chace"}


class TestGivenAnAdmin:
    @pytest.fixture
    async def request_sent(self) -> dict[str, Any]:
        model = FakeModel(message(text("Which month?")))
        await turn(model, FakeLedger(admin=True), "show everyone's spending")
        return model.requests[0]

    async def test_list_all_transactions_is_offered_too_but_still_no_edit_or_delete(
        self, request_sent: dict[str, Any]
    ) -> None:
        assert [t["name"] for t in request_sent["tools"]] == [
            "create_transaction",
            "list_transactions",
            "get_monthly_summary",
            "list_all_transactions",
        ]

    async def test_the_prompt_explains_it_and_keeps_recording_to_their_own_ledger(
        self, request_sent: dict[str, Any]
    ) -> None:
        system = request_sent["system"]
        assert "list_all_transactions" in system
        assert "username" in system
        assert "only in their own ledger" in system


class TestGivenAnAdminAsksForEveryonesSpending:
    async def test_everyones_transactions_come_back_as_data_with_whose_they_are(self) -> None:
        model = FakeModel(
            message(tool_use("t1", "list_all_transactions", OCTOBER), stop_reason="tool_use"),
            message(text("Here are October's 2 expenses across everyone, $20.00 in total.")),
        )
        ledger = FakeLedger({"list_all_transactions": page(HAORAN_COFFEE, CHACE_LUNCH)}, admin=True)
        result = await turn(model, ledger, "show all of our spending this month")
        assert ledger.calls == [("list_all_transactions", OCTOBER)]
        assert result.listed == [HAORAN_COFFEE, CHACE_LUNCH]
        assert result.transactions == []

import json
from typing import Any

import pytest

from ledger_agent.agent import ModelUnavailable
from ledger_agent.handler import MAX_IMAGE_LENGTH
from ledger_agent.ledger import LedgerUnavailable, ToolSpec
from ledger_agent.receipts import Reading

from .fakes import FakeLedger, FakeModel, message, text
from .test_handler import ALICE_TOKEN, NOW, Harness, error_code
from .test_receipts import IMAGE, reading

ROUTE = "POST /agent/v1/receipts/read"
BODY = {"image": IMAGE, "mediaType": "image/jpeg", "timeZone": "Australia/Melbourne"}


def on_the_day(**fields: Any) -> Reading:
    """A receipt dated today in Melbourne, which is the 28th at the harness's clock."""
    return reading(printedDate="28/10/2026", **fields)


class DownLedger(FakeLedger):
    async def list_tools(self) -> list[ToolSpec]:
        raise LedgerUnavailable("down")


def harness(*answers: Any, ledger: FakeLedger | None = None) -> Harness:
    return Harness(FakeModel(*(answers or (message(text(on_the_day().model_dump_json())),))), ledger)


async def read(h: Harness, body: Any = None, **kwargs: Any) -> tuple[int, dict[str, Any]]:
    return await h.post(BODY if body is None else body, route=ROUTE, **kwargs)


class TestGivenAPhotoOfAReceipt:
    async def test_returns_it_checked(self) -> None:
        status, body = await read(harness())

        assert status == 200
        assert body["canRecord"] is True
        assert body["problems"] == []
        assert body["receipt"]["paidCents"] == 50

    async def test_counts_it_for_the_customer_not_as_a_chat_message(self) -> None:
        h = harness()

        await read(h)

        assert h.receipts_counted == [("alice", NOW)]
        assert h.counted == []

    async def test_says_when_it_needs_a_look(self) -> None:
        answer = on_the_day(lines=[]).model_dump_json()

        _, body = await read(harness(message(text(answer))))

        assert body["canRecord"] is False
        assert body["problems"] == ["lines_dont_add_up"]

    async def test_uses_the_customers_own_date(self) -> None:
        # 13:30 UTC on the 27th is the 28th in Melbourne, so a receipt dated the 28th isn't in the future.
        _, body = await read(harness())

        assert body["problems"] == []


class TestGivenATokenTheLedgerRejects:
    async def test_refuses_before_counting_or_calling_claude(self) -> None:
        h = harness(ledger=FakeLedger(authorized=False))

        status, body = await read(h)

        assert (status, error_code(body)) == (401, "unauthorized")
        assert h.receipts_counted == []
        assert h.model.requests == []


class TestGivenNoBearerToken:
    async def test_refuses_it(self) -> None:
        status, body = await read(harness(), authorization=None)

        assert (status, error_code(body)) == (401, "unauthorized")


class TestGivenThePhotoCannotBeUsed:
    @pytest.mark.parametrize(
        ("fields", "code"),
        [
            pytest.param({"isReceipt": False}, "not_a_receipt", id="not-a-receipt"),
            pytest.param({"readable": False}, "unreadable", id="unreadable"),
        ],
    )
    async def test_says_why_and_it_still_counts(self, fields: dict[str, Any], code: str) -> None:
        h = harness(message(text(reading(**fields).model_dump_json())))

        status, body = await read(h)

        assert (status, error_code(body)) == (422, code)
        assert len(h.receipts_counted) == 1

    async def test_says_when_a_receipt_is_too_long(self) -> None:
        status, body = await read(harness(message(text("{"), stop_reason="max_tokens")))

        assert (status, error_code(body)) == (422, "receipt_too_long")


class TestGivenAnImageThatIsTooBig:
    async def test_refuses_it_without_calling_claude(self) -> None:
        h = harness()

        status, body = await read(h, {**BODY, "image": "A" * (MAX_IMAGE_LENGTH + 4)})

        assert (status, error_code(body)) == (413, "image_too_large")
        assert h.model.requests == []
        assert h.receipts_counted == []


class TestGivenABadRequest:
    @pytest.mark.parametrize(
        ("body", "code"),
        [
            pytest.param({**BODY, "mediaType": "application/pdf"}, "validation_failed", id="a-pdf"),
            pytest.param({"mediaType": "image/jpeg", "timeZone": "UTC"}, "validation_failed", id="no-image"),
            pytest.param({**BODY, "image": "not base64!"}, "validation_failed", id="not-base64"),
            pytest.param({**BODY, "timeZone": "Mars/Base"}, "invalid_time_zone", id="unknown-time-zone"),
        ],
    )
    async def test_refuses_it_without_calling_claude(self, body: dict[str, Any], code: str) -> None:
        h = harness()

        status, res = await read(h, body)

        assert (status, error_code(res)) == (400, code)
        assert h.model.requests == []

    async def test_refuses_a_body_that_is_not_json(self) -> None:
        status, body = await harness().post(raw="{", route=ROUTE)

        assert (status, error_code(body)) == (400, "invalid_json")


class TestGivenTheCustomerHasUsedTodaysReceipts:
    async def test_says_so_without_calling_claude(self) -> None:
        h = harness()
        h.receipts_left = False

        status, body = await read(h)

        assert (status, error_code(body)) == (429, "daily_receipt_limit_reached")
        assert h.model.requests == []


class TestGivenEveryoneHasUsedTodaysTotal:
    async def test_says_so_without_calling_claude(self) -> None:
        h = harness()
        h.total_left = False

        status, body = await read(h)

        assert (status, error_code(body)) == (429, "daily_total_reached")


class TestGivenADependencyIsDown:
    async def test_the_ledger_is_a_502(self) -> None:
        status, body = await read(harness(ledger=DownLedger()))

        assert (status, error_code(body)) == (502, "ledger_unavailable")

    async def test_claude_is_a_503(self) -> None:
        status, body = await read(harness(ModelUnavailable("down")))

        assert (status, error_code(body)) == (503, "model_unavailable")

    async def test_anything_else_is_a_500(self) -> None:
        status, body = await read(harness(ValueError("boom")))

        assert (status, error_code(body)) == (500, "internal_error")


class TestLogging:
    async def test_never_logs_the_image_or_the_token(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level("INFO", logger="ledger_agent")

        await read(harness())

        logged = json.dumps([record.__dict__ for record in caplog.records], default=str)
        assert IMAGE not in logged
        assert ALICE_TOKEN not in logged
        assert "receipt_read" in logged

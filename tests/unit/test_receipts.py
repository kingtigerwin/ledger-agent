from datetime import date
from typing import Any

import pytest

from ledger_agent.agent import MODEL_ID, ModelUnavailable
from ledger_agent.receipts import (
    MAX_TOKENS,
    Problem,
    Reading,
    ReceiptNotRead,
    bill_from,
    check_receipt,
    read_receipt,
)

from .fakes import FakeModel, message, text

TODAY = date(2026, 11, 1)


def reading(**fields: Any) -> Reading:
    """The Flybuys receipt, as Claude reads it."""
    return Reading.model_validate(
        {
            "kind": "receipt",
            "readable": True,
            "merchant": "Coles",
            "location": "The Glen",
            "abn": "45004189708",
            "receiptNumber": "6547",
            "printedDate": "01/11/2026",
            "printedTime": "15:06",
            "printedDueDate": None,
            "billFor": None,
            "category": "groceries",
            "totalCents": 3050,
            "gstCents": 0,
            "savingsCents": 3100,
            "payments": [
                {"label": "Flybuys value redeemed", "kind": "points", "amountCents": 3000},
                {"label": "EFT", "kind": "money", "amountCents": 50},
            ],
            "lines": [
                {"name": "PI PERFECT BAKES 450", "product": "pasta bake", "amountCents": 850},
                {"name": "COLES BEEF MINCE 4ST", "product": "beef mince", "amountCents": 1550},
                {"name": "CLS CHICKEN MINCE", "product": "chicken mince", "amountCents": 650},
            ],
            **fields,
        }
    )


def card_only(total: int = 1774) -> dict[str, Any]:
    return {
        "totalCents": total,
        "payments": [{"label": "EFTPOS", "kind": "money", "amountCents": total}],
        "lines": [{"name": "B.BARRAMUNDI", "product": "barramundi", "amountCents": total}],
    }


def problems(**fields: Any) -> list[Problem]:
    return check_receipt(reading(**fields), TODAY)["problems"]


class TestGivenAReceiptThatAddsUp:
    def test_it_can_be_recorded(self) -> None:
        checked = check_receipt(reading(), TODAY)

        assert checked["problems"] == []
        assert checked["canRecord"] is True

    def test_money_paid_is_the_amount_and_points_are_not(self) -> None:
        receipt = check_receipt(reading(), TODAY)["receipt"]

        assert (receipt["paidCents"], receipt["totalCents"]) == (50, 3050)

    def test_the_date_is_read_day_first(self) -> None:
        assert check_receipt(reading(), TODAY)["receipt"]["date"] == "2026-11-01"


class TestDates:
    @pytest.mark.parametrize(
        ("printed", "expected"),
        [
            pytest.param("01/11/2026", "2026-11-01", id="coles-full"),
            pytest.param("1/11/2026", "2026-11-01", id="tan-hung-no-leading-zero"),
            pytest.param("01/11/26", "2026-11-01", id="two-digit-year"),
            pytest.param("01-11-2026", "2026-11-01", id="dashes"),
            pytest.param("01.11.26", "2026-11-01", id="dots"),
            pytest.param("1 Nov 2026", "2026-11-01", id="month-name"),
            pytest.param("1 November 2026", "2026-11-01", id="long-month-name"),
        ],
    )
    def test_reads_the_ways_a_date_gets_printed(self, printed: str, expected: str) -> None:
        checked = check_receipt(reading(printedDate=printed), TODAY)

        assert (checked["receipt"]["date"], checked["problems"]) == (expected, [])

    @pytest.mark.parametrize(
        "printed",
        [
            pytest.param(None, id="missing"),
            pytest.param("smudged", id="not-a-date"),
            pytest.param("31/02/2026", id="no-such-day"),
            pytest.param("11/13/2026", id="american"),
        ],
    )
    def test_an_unreadable_date_needs_a_look(self, printed: str | None) -> None:
        checked = check_receipt(reading(printedDate=printed), TODAY)

        assert checked["receipt"]["date"] is None
        assert checked["problems"] == ["date_unreadable"]

    def test_a_future_date_needs_a_look(self) -> None:
        assert problems(printedDate="02/11/2026") == ["date_in_future"]

    def test_a_date_over_a_year_old_needs_a_look(self) -> None:
        assert problems(printedDate="01/10/2025") == ["date_too_old"]


class TestTimes:
    @pytest.mark.parametrize(
        ("printed", "expected"),
        [
            pytest.param("15:06", "15:06", id="24-hour"),
            pytest.param("14:19:45", "14:19", id="with-seconds"),
            pytest.param("02:17 PM", "14:17", id="afternoon"),
            pytest.param("12:05 AM", "00:05", id="just-after-midnight"),
            pytest.param("12:05 PM", "12:05", id="just-after-noon"),
            pytest.param("9:05 am", "09:05", id="lower-case-morning"),
            pytest.param("25:00", None, id="no-such-time"),
            pytest.param(None, None, id="missing"),
        ],
    )
    def test_keeps_a_valid_time_as_24_hours(self, printed: str | None, expected: str | None) -> None:
        assert check_receipt(reading(printedTime=printed), TODAY)["receipt"]["time"] == expected


class TestSums:
    def test_lines_that_dont_add_up_need_a_look(self) -> None:
        lines = [{"name": "ONE", "product": "one", "amountCents": 100}]

        assert problems(lines=lines) == ["lines_dont_add_up"]

    def test_a_discount_line_counts_against_the_total(self) -> None:
        lines = [
            {"name": "ONE", "product": "one", "amountCents": 3250},
            {"name": "MEMBER DISCOUNT", "product": "discount", "amountCents": -200},
        ]

        assert problems(lines=lines) == []

    def test_payments_that_dont_add_up_need_a_look_and_assume_the_total_was_paid(self) -> None:
        payments = [{"label": "EFT", "kind": "money", "amountCents": 50}]

        checked = check_receipt(reading(payments=payments), TODAY)

        assert checked["problems"] == ["payments_dont_add_up"]
        assert checked["receipt"]["paidCents"] == 3050

    def test_no_payments_at_all_need_a_look(self) -> None:
        assert problems(payments=[]) == ["payments_dont_add_up"]


class TestGivenAReceiptPaidEntirelyWithPoints:
    def test_there_is_nothing_to_record(self) -> None:
        payments = [{"label": "Flybuys", "kind": "points", "amountCents": 3050}]

        checked = check_receipt(reading(payments=payments), TODAY)

        assert checked["problems"] == ["nothing_paid"]
        assert checked["canRecord"] is False
        assert checked["receipt"]["paidCents"] == 0


class TestGivenNoMerchant:
    def test_it_needs_a_look(self) -> None:
        assert problems(merchant=None) == ["merchant_unreadable"]


def bill(**fields: Any) -> Reading:
    """A council rates notice, as Claude reads it."""
    return reading(
        **{
            "kind": "bill",
            "merchant": "Whitehorse City Council",
            "location": None,
            "abn": "39549568822",
            "receiptNumber": None,
            "printedDate": None,
            "printedTime": None,
            "printedDueDate": "30 November 2026",
            "billFor": "council rates",
            "category": "utilities",
            "totalCents": 58300,
            "gstCents": None,
            "savingsCents": None,
            "payments": [],
            "lines": [],
            **fields,
        }
    )


class TestGivenABill:
    def test_it_is_what_is_due_and_who_to(self) -> None:
        assert bill_from(bill()) == {
            "biller": "Whitehorse City Council",
            "item": "council rates",
            "category": "utilities",
            "amountCents": 58300,
            "dueDate": "2026-11-30",
        }

    def test_a_due_date_that_cannot_be_read_is_left_out(self) -> None:
        assert bill_from(bill(printedDueDate="soon"))["dueDate"] is None

    @pytest.mark.parametrize("field", ["merchant", "billFor"])
    def test_blank_text_is_left_out(self, field: str) -> None:
        assert None in bill_from(bill(**{field: " "})).values()

    def test_long_text_is_cut_to_what_ledger_api_accepts(self) -> None:
        read = bill_from(bill(merchant="X" * 300, billFor="y" * 300))

        assert (len(read["biller"] or ""), len(read["item"] or "")) == (100, 100)


class TestWhatItKeeps:
    def test_an_abn_is_digits_only(self) -> None:
        assert check_receipt(reading(abn="45 004 189 708"), TODAY)["receipt"]["abn"] == "45004189708"

    def test_an_abn_of_the_wrong_length_is_dropped(self) -> None:
        assert check_receipt(reading(abn="4500"), TODAY)["receipt"]["abn"] is None

    def test_a_receipt_number_is_kept_as_printed(self) -> None:
        assert check_receipt(reading(), TODAY)["receipt"]["receiptNumber"] == "6547"

    def test_long_text_is_cut_to_what_ledger_api_accepts(self) -> None:
        lines = [{"name": "X" * 300, "product": "y" * 300, "amountCents": 3050}]

        [line] = check_receipt(reading(lines=lines), TODAY)["receipt"]["lines"]

        assert (len(line["name"]), len(line.get("product", ""))) == (200, 100)

    def test_a_blank_product_is_left_out(self) -> None:
        lines = [{"name": "MISC ITEM", "product": " ", "amountCents": 3050}]

        [line] = check_receipt(reading(lines=lines), TODAY)["receipt"]["lines"]

        assert line == {"name": "MISC ITEM", "amountCents": 3050}


READING_JSON = reading().model_dump_json()
IMAGE = "aW1hZ2U="


class TestReadReceipt:
    async def test_sends_the_image_to_claude_with_no_tools(self) -> None:
        model = FakeModel(message(text(READING_JSON)))

        await read_receipt(model, IMAGE, "image/jpeg")

        [request] = model.requests
        assert request["model"] == MODEL_ID
        assert request["max_tokens"] == MAX_TOKENS
        assert "tools" not in request
        assert request["output_config"]["format"]["type"] == "json_schema"
        image = request["messages"][0]["content"][0]
        assert image["source"] == {"type": "base64", "media_type": "image/jpeg", "data": IMAGE}

    async def test_returns_what_claude_read(self) -> None:
        read = await read_receipt(FakeModel(message(text(READING_JSON))), IMAGE, "image/jpeg")

        assert read.total_cents == 3050

    async def test_returns_a_bill_rather_than_refusing_it(self) -> None:
        read = await read_receipt(FakeModel(message(text(bill().model_dump_json()))), IMAGE, "image/jpeg")

        assert (read.kind, read.total_cents) == ("bill", 58300)

    async def test_a_bill_with_no_readable_amount_is_unreadable(self) -> None:
        answer = bill(totalCents=None).model_dump_json()

        with pytest.raises(ReceiptNotRead) as caught:
            await read_receipt(FakeModel(message(text(answer))), IMAGE, "image/jpeg")

        assert caught.value.code == "unreadable"

    async def test_a_photo_of_something_else_is_not_a_receipt(self) -> None:
        answer = reading(kind="other").model_dump_json()

        with pytest.raises(ReceiptNotRead) as caught:
            await read_receipt(FakeModel(message(text(answer))), IMAGE, "image/jpeg")

        assert caught.value.code == "not_a_receipt"

    @pytest.mark.parametrize(
        "fields",
        [pytest.param({"readable": False}, id="said-so"), pytest.param({"totalCents": None}, id="no-total")],
    )
    async def test_a_receipt_with_no_readable_total_is_unreadable(self, fields: dict[str, Any]) -> None:
        answer = reading(**fields).model_dump_json()

        with pytest.raises(ReceiptNotRead) as caught:
            await read_receipt(FakeModel(message(text(answer))), IMAGE, "image/jpeg")

        assert caught.value.code == "unreadable"

    async def test_a_refusal_is_unreadable(self) -> None:
        with pytest.raises(ReceiptNotRead) as caught:
            await read_receipt(FakeModel(message(stop_reason="refusal")), IMAGE, "image/jpeg")

        assert caught.value.code == "unreadable"

    async def test_a_receipt_too_long_to_finish_says_so(self) -> None:
        with pytest.raises(ReceiptNotRead) as caught:
            await read_receipt(FakeModel(message(text("{"), stop_reason="max_tokens")), IMAGE, "image/jpeg")

        assert caught.value.code == "receipt_too_long"

    async def test_lets_a_failed_call_through(self) -> None:
        with pytest.raises(ModelUnavailable):
            await read_receipt(FakeModel(ModelUnavailable("down")), IMAGE, "image/jpeg")

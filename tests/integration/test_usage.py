"""The daily allowance against DynamoDB Local (`docker run -p 8000:8000 amazon/dynamodb-local`)."""

import os
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import boto3
import pytest

from ledger_agent.usage import (
    DAILY_MESSAGE_LIMIT,
    DAILY_TOTAL_LIMIT,
    DailyLimitReached,
    DailyTotalReached,
    count_message,
)

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import Table

# 13:30 UTC on the 27th is already the 28th in Sydney; 12:59 UTC on the 27th is still 23:59 there.
BEFORE_MIDNIGHT = datetime(2026, 10, 27, 12, 59, tzinfo=UTC)
AFTER_MIDNIGHT = datetime(2026, 10, 27, 13, 30, tzinfo=UTC)


@pytest.fixture
def table() -> Iterator["Table"]:
    dynamodb = boto3.resource(  # pyright: ignore[reportUnknownMemberType]
        "dynamodb",
        endpoint_url=os.environ.get("DYNAMODB_ENDPOINT", "http://localhost:8000"),
        region_name="ap-southeast-2",
        aws_access_key_id="local",
        aws_secret_access_key="local",
    )
    created = dynamodb.create_table(
        TableName=f"usage-{uuid.uuid4()}",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    yield created
    created.delete()


def use_up(table: "Table", user_id: str, now: datetime) -> None:
    for _ in range(DAILY_MESSAGE_LIMIT):
        count_message(table, user_id, now)


class TestGivenACustomerWithinTheirAllowance:
    def test_it_lets_them_send_exactly_the_daily_limit(self, table: "Table") -> None:
        use_up(table, "alice", BEFORE_MIDNIGHT)

    def test_it_expires_the_count_once_the_day_is_over(self, table: "Table") -> None:
        count_message(table, "alice", BEFORE_MIDNIGHT)
        item = table.scan()["Items"][0]
        assert int(str(item["expiresAt"])) > BEFORE_MIDNIGHT.timestamp() + 24 * 60 * 60


class TestGivenACustomerWhoHasUsedTheirAllowance:
    def test_it_refuses_the_next_message(self, table: "Table") -> None:
        use_up(table, "alice", BEFORE_MIDNIGHT)
        with pytest.raises(DailyLimitReached):
            count_message(table, "alice", BEFORE_MIDNIGHT)

    def test_it_leaves_other_customers_alone(self, table: "Table") -> None:
        use_up(table, "alice", BEFORE_MIDNIGHT)
        count_message(table, "bob", BEFORE_MIDNIGHT)

    def test_it_starts_again_at_midnight_in_sydney(self, table: "Table") -> None:
        use_up(table, "alice", BEFORE_MIDNIGHT)
        count_message(table, "alice", AFTER_MIDNIGHT)


def use_up_the_total(table: "Table", now: datetime) -> None:
    for number in range(DAILY_TOTAL_LIMIT):
        count_message(table, f"user-{number}", now)


class TestGivenEveryoneTogetherHasUsedTheDailyTotal:
    def test_it_refuses_anyone_with_messages_of_their_own_left(self, table: "Table") -> None:
        use_up_the_total(table, BEFORE_MIDNIGHT)
        with pytest.raises(DailyTotalReached):
            count_message(table, "alice", BEFORE_MIDNIGHT)

    def test_it_starts_again_at_midnight_in_sydney(self, table: "Table") -> None:
        use_up_the_total(table, BEFORE_MIDNIGHT)
        count_message(table, "alice", AFTER_MIDNIGHT)


class TestGivenACustomerPastTheirOwnLimit:
    def test_their_refused_messages_dont_use_up_the_total(self, table: "Table") -> None:
        use_up(table, "alice", BEFORE_MIDNIGHT)
        for _ in range(3):
            with pytest.raises(DailyLimitReached):
                count_message(table, "alice", BEFORE_MIDNIGHT)
        total = table.get_item(Key={"pk": "TOTAL#2026-10-27"}).get("Item", {})
        assert total["messages"] == DAILY_MESSAGE_LIMIT


class TestGivenTheSmokeTestUser:
    def test_it_isnt_limited_or_counted(self, table: "Table") -> None:
        for _ in range(DAILY_MESSAGE_LIMIT + 1):
            count_message(table, "agent-e2e", BEFORE_MIDNIGHT, exempt="agent-e2e")
        assert table.scan()["Items"] == []

    def test_everyone_else_still_is(self, table: "Table") -> None:
        use_up(table, "alice", BEFORE_MIDNIGHT)
        with pytest.raises(DailyLimitReached):
            count_message(table, "alice", BEFORE_MIDNIGHT, exempt="agent-e2e")


class TestGivenMoreMessagesAtOnceThanAreLeft:
    def test_it_lets_through_only_the_allowance(self, table: "Table") -> None:
        def attempt(_: int) -> bool:
            try:
                count_message(table, "alice", BEFORE_MIDNIGHT)
            except DailyLimitReached:
                return False
            return True

        with ThreadPoolExecutor(max_workers=8) as pool:
            accepted = sum(pool.map(attempt, range(DAILY_MESSAGE_LIMIT + 10)))
        assert accepted == DAILY_MESSAGE_LIMIT

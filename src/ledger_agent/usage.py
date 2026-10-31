"""Each customer's daily allowance of messages: a message can cost several Claude calls, the one paid part."""

import base64
import binascii
import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from .clock import today_in
from .ledger import Unauthorized

if TYPE_CHECKING:
    # Dev-only stubs; not installed on Lambda.
    from mypy_boto3_dynamodb.service_resource import Table

DAILY_MESSAGE_LIMIT = 50
# One zone for everyone, not the customer's: the UI sends that one, so changing it could buy extra days.
RESET_ZONE = "Australia/Sydney"
# Past the end of the day in any case. The date is in the key, so a late expiry changes nothing.
_KEEP_FOR = timedelta(days=2)


class DailyLimitReached(Exception):
    """The customer has used today's messages."""


def user_id_from(token: str) -> str:
    """Who an access token belongs to. Only for a token ledger-api has just accepted: it's the one checking the
    signature, and the agent doesn't hold the secret to."""
    try:
        payload = token.split(".")[1]
        claims: Any = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError, binascii.Error) as err:
        raise Unauthorized("the access token has no readable claims") from err
    subject = cast(dict[str, Any], claims).get("sub") if isinstance(claims, dict) else None
    if not isinstance(subject, str) or not subject:
        raise Unauthorized("the access token doesn't say who it's for")
    return subject


def count_message(table: "Table", user_id: str, now: datetime) -> None:
    """Counts one message, or raises DailyLimitReached. One conditional write, so messages sent at the same moment
    can't get past the limit together."""
    day = today_in(RESET_ZONE, now).isoformat()
    try:
        table.update_item(
            Key={"pk": f"USER#{user_id}#{day}"},
            UpdateExpression="ADD messages :one SET expiresAt = :expires",
            ConditionExpression="attribute_not_exists(messages) OR messages < :limit",
            ExpressionAttributeValues={
                ":one": 1,
                ":limit": DAILY_MESSAGE_LIMIT,
                ":expires": int((now + _KEEP_FOR).timestamp()),
            },
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException as err:
        raise DailyLimitReached from err

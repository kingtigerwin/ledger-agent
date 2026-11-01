"""The HTTP API (payload v2): POST /agent/v1/chat runs one agent turn, POST /agent/v1/receipts/read reads a photo."""

import base64
import binascii
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .agent import ChatMessage, Model, ModelUnavailable, run_turn
from .clock import InvalidTimeZone, today_in
from .ledger import Ledger, LedgerUnavailable, Unauthorized
from .receipts import MediaType, ReceiptNotRead, check_receipt, read_receipt
from .usage import (
    DAILY_MESSAGE_LIMIT,
    DAILY_RECEIPT_LIMIT,
    DailyLimitReached,
    DailyReceiptLimitReached,
    DailyTotalReached,
    user_id_from,
)

# Bounds on what one request can make Claude read, since Claude is the part that costs money.
MAX_MESSAGES = 20
MAX_CONTENT_LENGTH = 2000
# Base64 characters, about 1.1 MB of image: a receipt shrunk in the browser is far smaller, and this stays well
# inside Lambda's 6 MB request limit.
MAX_IMAGE_LENGTH = 1_500_000

READ_RECEIPT_ROUTE = "POST /agent/v1/receipts/read"

_NOT_READ = {
    "not_a_receipt": "That doesn't look like a receipt. Try another photo.",
    "unreadable": "That receipt couldn't be read. Try a clearer photo in good light.",
    "receipt_too_long": "That receipt is too long to read in one go. You can add it in the chat instead.",
}

log = logging.getLogger("ledger_agent")

type Event = dict[str, Any]
type Response = dict[str, Any]


@dataclass(frozen=True)
class Deps:
    model: Model
    connect: Callable[[str], AbstractAsyncContextManager[Ledger]]
    now: Callable[[], datetime]
    # Raises DailyLimitReached once the customer has used today's messages.
    count_message: Callable[[str, datetime], None]
    # Raises DailyReceiptLimitReached once the customer has used today's receipts.
    count_receipt: Callable[[str, datetime], None]


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class _Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=MAX_CONTENT_LENGTH)


class _ChatRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    messages: list[_Message] = Field(min_length=1, max_length=MAX_MESSAGES)
    time_zone: str = Field(alias="timeZone")

    @model_validator(mode="after")
    def _ends_with_the_customer(self) -> "_ChatRequest":
        if self.messages[-1].role != "user":
            raise ValueError("the last message must be the customer's")
        return self


class _ReceiptRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    image: str = Field(min_length=1)
    # Photos only. The browser re-encodes them as JPEG; the others are for a client that doesn't.
    media_type: MediaType = Field(alias="mediaType")
    time_zone: str = Field(alias="timeZone")

    @field_validator("image")
    @classmethod
    def _is_base64(cls, value: str) -> str:
        try:
            base64.b64decode(value, validate=True)
        except binascii.Error as err:
            raise ValueError("must be base64") from err
        return value


def create_handler(deps: Deps) -> Callable[[Event], Awaitable[Response]]:
    async def chat(event: Event) -> Response:
        token = _bearer_token(event)
        request = _parse(event, _ChatRequest)
        today = _today(request.time_zone, deps.now())
        messages = [ChatMessage(role=m.role, content=m.content) for m in request.messages]
        async with deps.connect(token) as ledger:
            result = await run_turn(
                model=deps.model,
                ledger=ledger,
                messages=messages,
                today=today,
                admit=lambda: deps.count_message(user_id_from(token), deps.now()),
            )
        log.info(
            "turn_finished",
            extra={
                "transactions": len(result.transactions),
                "listed": len(result.listed),
                "summaries": len(result.summaries),
            },
        )
        return _json(
            200,
            {
                "reply": result.reply,
                "transactions": result.transactions,
                "listed": result.listed,
                "summaries": result.summaries,
            },
        )

    async def read_a_receipt(event: Event) -> Response:
        """Saves nothing: the UI records the result, or asks the customer to look at it first."""
        token = _bearer_token(event)
        request = _parse(event, _ReceiptRequest)
        if len(request.image) > MAX_IMAGE_LENGTH:
            raise ApiError(413, "image_too_large", "That photo is too large; try a smaller one.")
        today = _today(request.time_zone, deps.now())
        # Only real customers are counted, and only they cost a Claude call.
        async with deps.connect(token) as ledger:
            await ledger.list_tools()
        deps.count_receipt(user_id_from(token), deps.now())
        checked = check_receipt(await read_receipt(deps.model, request.image, request.media_type), today)
        log.info("receipt_read", extra={"problems": checked["problems"], "lines": len(checked["receipt"]["lines"])})
        return _json(200, dict(checked))

    async def handle(event: Event) -> Response:
        try:
            return await (read_a_receipt if event.get("routeKey") == READ_RECEIPT_ROUTE else chat)(event)
        except ApiError as err:
            return _error(err.status, err.code, err.message)
        except Unauthorized:
            return _error(401, "unauthorized", "Sign in again.")
        except DailyLimitReached:
            log.info("daily_limit_reached")
            message = (
                f"You've used today's {DAILY_MESSAGE_LIMIT} messages. You can send more after midnight Sydney time."
            )
            return _error(429, "daily_limit_reached", message)
        except DailyReceiptLimitReached:
            log.info("daily_receipt_limit_reached")
            message = (
                f"You've scanned today's {DAILY_RECEIPT_LIMIT} receipts. You can scan more after midnight Sydney time."
            )
            return _error(429, "daily_receipt_limit_reached", message)
        except DailyTotalReached:
            log.warning("daily_total_reached")
            return _error(
                429, "daily_total_reached", "The chat is very busy today. It'll be back after midnight Sydney time."
            )
        except ReceiptNotRead as err:
            log.info("receipt_not_read", extra={"reason": err.code})
            return _error(422, err.code, _NOT_READ[err.code])
        except LedgerUnavailable as err:
            log.warning("ledger_unavailable", extra={"reason": str(err)})
            return _error(502, "ledger_unavailable", "The ledger can't be reached; try again shortly.")
        except ModelUnavailable as err:
            log.warning("model_unavailable", extra={"reason": str(err)})
            return _error(503, "model_unavailable", "The assistant is unavailable; try again shortly.")
        except Exception:
            log.exception("turn_failed")
            return _error(500, "internal_error", "Something went wrong; try again.")

    return handle


def _today(time_zone: str, now: datetime) -> date:
    try:
        return today_in(time_zone, now)
    except InvalidTimeZone as err:
        raise ApiError(400, "invalid_time_zone", str(err)) from err


def _bearer_token(event: Event) -> str:
    # HTTP API lower-cases header names in payload v2.
    headers: dict[str, str] = event.get("headers") or {}
    authorization = headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme != "Bearer" or not token.strip():
        raise ApiError(401, "unauthorized", "Send the customer's ledger-api access token as a Bearer token.")
    return token.strip()


def _parse[T: BaseModel](event: Event, schema: type[T]) -> T:
    body: str = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as err:
        raise ApiError(400, "invalid_json", "The body must be JSON.") from err
    try:
        return schema.model_validate(payload)
    except ValidationError as err:
        first = err.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        raise ApiError(400, "validation_failed", f"{where}: {first['msg']}" if where else first["msg"]) from err


def _error(status: int, code: str, message: str) -> Response:
    return _json(status, {"error": {"code": code, "message": message}})


def _json(status: int, body: dict[str, Any]) -> Response:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False),
    }

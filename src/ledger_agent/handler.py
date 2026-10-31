"""POST /agent/v1/chat: API Gateway HTTP API (payload v2) in, one agent turn, JSON out."""

import base64
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .agent import ChatMessage, Model, ModelUnavailable, run_turn
from .clock import InvalidTimeZone, today_in
from .ledger import Ledger, LedgerUnavailable, Unauthorized
from .usage import DAILY_MESSAGE_LIMIT, DailyLimitReached, DailyTotalReached, user_id_from

# Bounds on what one request can make Claude read, since Claude is the part that costs money.
MAX_MESSAGES = 20
MAX_CONTENT_LENGTH = 2000

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


def create_handler(deps: Deps) -> Callable[[Event], Awaitable[Response]]:
    async def handle(event: Event) -> Response:
        try:
            token = _bearer_token(event)
            request = _parse(event)
            try:
                today = today_in(request.time_zone, deps.now())
            except InvalidTimeZone as err:
                raise ApiError(400, "invalid_time_zone", str(err)) from err
            messages = [ChatMessage(role=m.role, content=m.content) for m in request.messages]
            async with deps.connect(token) as ledger:
                result = await run_turn(
                    model=deps.model,
                    ledger=ledger,
                    messages=messages,
                    today=today,
                    admit=lambda: deps.count_message(user_id_from(token), deps.now()),
                )
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
        except DailyTotalReached:
            log.warning("daily_total_reached")
            return _error(
                429, "daily_total_reached", "The chat is very busy today. It'll be back after midnight Sydney time."
            )
        except LedgerUnavailable as err:
            log.warning("ledger_unavailable", extra={"reason": str(err)})
            return _error(502, "ledger_unavailable", "The ledger can't be reached; try again shortly.")
        except ModelUnavailable as err:
            log.warning("model_unavailable", extra={"reason": str(err)})
            return _error(503, "model_unavailable", "The assistant is unavailable; try again shortly.")
        except Exception:
            log.exception("turn_failed")
            return _error(500, "internal_error", "Something went wrong; try again.")
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

    return handle


def _bearer_token(event: Event) -> str:
    # HTTP API lower-cases header names in payload v2.
    headers: dict[str, str] = event.get("headers") or {}
    authorization = headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme != "Bearer" or not token.strip():
        raise ApiError(401, "unauthorized", "Send the customer's ledger-api access token as a Bearer token.")
    return token.strip()


def _parse(event: Event) -> _ChatRequest:
    body: str = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as err:
        raise ApiError(400, "invalid_json", "The body must be JSON.") from err
    try:
        return _ChatRequest.model_validate(payload)
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

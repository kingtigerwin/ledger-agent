"""Smoke test of the deployed agent: one real turn that records a transaction, checked over ledger-api, then deleted.

    AGENT_URL=https://agent.haoran.cloud uv run python scripts/smoke.py

Uses one ledger-api user, `agent-e2e`, whose password is the SSM parameter /ledger-agent/e2e-password (made by hand).
On the first run the user is registered and the ledger-api admin approves it, which needs the admin password from
SSM; later runs only read the user's own password. Makes one real Claude call (a fraction of a cent).
"""

import os
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import boto3
import httpx2

if TYPE_CHECKING:
    from mypy_boto3_ssm import SSMClient

AGENT_URL = os.environ.get("AGENT_URL", "https://agent.haoran.cloud").rstrip("/")
LEDGER_URL = os.environ.get("LEDGER_URL", "https://api.haoran.cloud").rstrip("/")
REGION = "ap-southeast-2"
USERNAME = "agent-e2e"
PASSWORD_PARAMETER = "/ledger-agent/e2e-password"
ADMIN_USERNAME = "sisi"
ADMIN_PASSWORD_PARAMETER = "/ledger-api/admin-password"
TIME_ZONE = "Australia/Sydney"
# A distinctive amount, so the check can't match some other transaction.
MESSAGE = "I bought a coffee at Starbucks for $5.37"
AMOUNT_CENTS = 537


def main() -> None:
    http = httpx2.Client(timeout=30)
    password = secret(PASSWORD_PARAMETER)
    token = sign_in(http, password)

    rejected = chat(http, "not-a-real-token")
    expect("a rejected token is refused", rejected.status_code == 401, rejected)

    reply = chat(http, token)
    expect("the agent answers 200", reply.status_code == 200, reply)
    body: dict[str, Any] = reply.json()
    transactions: list[dict[str, Any]] = body["transactions"]
    try:
        expect("exactly one transaction was recorded", len(transactions) == 1, reply)
        [recorded] = transactions
        today = datetime.now(UTC).astimezone(ZoneInfo(TIME_ZONE)).date().isoformat()
        expect(
            "it is $5.37 AUD at Starbucks today",
            (recorded["amountCents"], recorded["currency"], recorded["date"]) == (AMOUNT_CENTS, "AUD", today)
            and "starbucks" in str(recorded.get("merchant", "")).lower(),
            reply,
        )
        stored = http.get(f"{LEDGER_URL}/api/v1/transactions/{recorded['id']}", headers=bearer(token))
        expect("ledger-api has it", stored.status_code == 200, stored)
        print(f"reply: {body['reply']}")
    finally:
        for transaction in transactions:
            deleted = http.delete(f"{LEDGER_URL}/api/v1/transactions/{transaction['id']}", headers=bearer(token))
            expect(f"deleted {transaction['id']}", deleted.status_code == 204, deleted)
    print("smoke test passed")


def sign_in(http: httpx2.Client, password: str) -> str:
    registered = http.post(f"{LEDGER_URL}/api/v1/auth/register", json={"username": USERNAME, "password": password})
    expect(f"register {USERNAME} (or already exists)", registered.status_code in (201, 409), registered)
    session = login(http, USERNAME, password)
    if session.status_code == 403:
        # First run: the user is pending until the admin enables it.
        admin = login(http, ADMIN_USERNAME, secret(ADMIN_PASSWORD_PARAMETER))
        expect("admin login", admin.status_code == 200, admin)
        admin_token = admin.json()["accessToken"]
        pending = http.get(f"{LEDGER_URL}/api/v1/admin/users?status=pending", headers=bearer(admin_token))
        expect("list pending users", pending.status_code == 200, pending)
        [user] = [u for u in pending.json()["items"] if u["username"] == USERNAME]
        enabled = http.post(f"{LEDGER_URL}/api/v1/admin/users/{user['id']}/enable", headers=bearer(admin_token))
        expect(f"enable {USERNAME}", enabled.status_code == 200, enabled)
        session = login(http, USERNAME, password)
    expect(f"{USERNAME} login", session.status_code == 200, session)
    return session.json()["accessToken"]


def login(http: httpx2.Client, username: str, password: str) -> httpx2.Response:
    return http.post(
        f"{LEDGER_URL}/api/v1/auth/login",
        json={"username": username, "password": password, "refreshTokenIn": "body"},
    )


def chat(http: httpx2.Client, token: str) -> httpx2.Response:
    return http.post(
        f"{AGENT_URL}/agent/v1/chat",
        headers=bearer(token),
        json={"messages": [{"role": "user", "content": MESSAGE}], "timeZone": TIME_ZONE},
    )


def secret(name: str) -> str:
    ssm: SSMClient = boto3.client("ssm", region_name=REGION)  # pyright: ignore[reportUnknownMemberType]
    value = ssm.get_parameter(Name=name, WithDecryption=True).get("Parameter", {}).get("Value")
    if not value:
        sys.exit(f"SSM parameter {name} has no value")
    return value


def bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def expect(what: str, ok: bool, response: httpx2.Response) -> None:
    if not ok:
        # Safe to print: a failing response is an error body or the agent's reply, never a successful login's tokens.
        sys.exit(f"FAILED: {what}: HTTP {response.status_code} {response.text[:500]}")
    print(f"ok: {what}")


if __name__ == "__main__":
    main()

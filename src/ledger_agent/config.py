import os
from typing import TYPE_CHECKING

import boto3

if TYPE_CHECKING:
    # Dev-only stubs; not installed on Lambda.
    from mypy_boto3_dynamodb.service_resource import Table
    from mypy_boto3_ssm import SSMClient

REGION = "ap-southeast-2"
API_KEY_PARAMETER = "/ledger-agent/anthropic-api-key"
LEDGER_MCP_URL = os.environ.get("LEDGER_MCP_URL", "https://api.haoran.cloud/api/mcp")
SMOKE_TEST_USER_ID = os.environ.get("SMOKE_TEST_USER_ID", "")


def usage_table() -> "Table":
    """The table counting each customer's messages; the stack names it."""
    return boto3.resource("dynamodb", region_name=REGION).Table(  # pyright: ignore[reportUnknownMemberType]
        os.environ["USAGE_TABLE"]
    )


def anthropic_api_key() -> str:
    """The Anthropic API key from SSM; it lives nowhere else."""
    # Only the SSM stubs are installed, so boto3.client's overloads for other services are unknown to pyright.
    ssm: SSMClient = boto3.client("ssm", region_name=REGION)  # pyright: ignore[reportUnknownMemberType]
    value = ssm.get_parameter(Name=API_KEY_PARAMETER, WithDecryption=True).get("Parameter", {}).get("Value")
    if not value:
        raise RuntimeError(f"SSM parameter {API_KEY_PARAMETER} has no value")
    return value

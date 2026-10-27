"""The LedgerAgent stack's resources, synthesized with placeholder code (no packaging)."""

import os

os.environ.setdefault("JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION", "1")

from collections.abc import Mapping
from typing import Any

import pytest
from aws_cdk import App, Environment
from aws_cdk import aws_lambda as lambda_
from aws_cdk.assertions import Match, Template

from infra.ledger_agent_stack import LedgerAgentStack

UI_ORIGINS = ["http://localhost:5173", "https://app.example.com"]


@pytest.fixture(scope="module")
def template() -> Template:
    app = App()
    stack = LedgerAgentStack(
        app,
        "LedgerAgent",
        code=lambda_.Code.from_inline("def handler(event, context): pass"),
        ui_origins=UI_ORIGINS,
        env=Environment(account="123456789012", region="ap-southeast-2"),
    )
    return Template.from_stack(stack)


def resources(template: Template, type_: str) -> Mapping[str, Mapping[str, Any]]:
    return template.find_resources(type_)


class TestTheFunction:
    def test_it_runs_python_3_13_on_arm(self, template: Template) -> None:
        template.has_resource_properties(
            "AWS::Lambda::Function",
            {
                "Runtime": "python3.13",
                "Architectures": ["arm64"],
                "Handler": "ledger_agent.lambda_function.handler",
                "Environment": {"Variables": {"LEDGER_MCP_URL": "https://api.haoran.cloud/api/mcp"}},
            },
        )

    def test_it_times_out_before_the_http_apis_30_second_limit(self, template: Template) -> None:
        [function] = resources(template, "AWS::Lambda::Function").values()
        assert function["Properties"]["Timeout"] < 30

    def test_it_can_read_only_the_anthropic_api_key(self, template: Template) -> None:
        [policy] = resources(template, "AWS::IAM::Policy").values()
        statements = policy["Properties"]["PolicyDocument"]["Statement"]
        assert len(statements) == 1
        assert statements[0]["Action"] == "ssm:GetParameter"
        assert "parameter/ledger-agent/anthropic-api-key" in str(statements[0]["Resource"])

    def test_its_logs_are_kept_two_weeks(self, template: Template) -> None:
        template.has_resource_properties("AWS::Logs::LogGroup", {"RetentionInDays": 14})


class TestTheHttpApi:
    def test_it_has_only_the_chat_route(self, template: Template) -> None:
        routes = [r["Properties"]["RouteKey"] for r in resources(template, "AWS::ApiGatewayV2::Route").values()]
        assert routes == ["POST /agent/v1/chat"]

    def test_it_allows_the_ui_origins_with_the_bearer_header(self, template: Template) -> None:
        template.has_resource_properties(
            "AWS::ApiGatewayV2::Api",
            {
                "CorsConfiguration": {
                    "AllowOrigins": UI_ORIGINS,
                    "AllowMethods": ["POST"],
                    "AllowHeaders": Match.array_with(["authorization", "content-type"]),
                }
            },
        )

    def test_its_stage_is_throttled(self, template: Template) -> None:
        [stage] = resources(template, "AWS::ApiGatewayV2::Stage").values()
        settings = stage["Properties"]["DefaultRouteSettings"]
        assert settings["ThrottlingRateLimit"] <= 5
        assert settings["ThrottlingBurstLimit"] <= 10

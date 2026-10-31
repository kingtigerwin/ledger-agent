"""The LedgerAgent stack's resources, synthesized with placeholder code (no packaging)."""

import os

os.environ.setdefault("JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION", "1")

from collections.abc import Mapping
from typing import Any

import pytest
from aws_cdk import App, Environment
from aws_cdk import aws_lambda as lambda_
from aws_cdk.assertions import Match, Template

from infra.ledger_agent_stack import CustomDomain, LedgerAgentStack

UI_ORIGINS = ["http://localhost:5173", "https://app.example.com"]


DOMAIN = CustomDomain(domain_name="agent.haoran.cloud", zone_name="haoran.cloud", hosted_zone_id="Z5UXKBB5IU578")


def synth(custom_domain: CustomDomain | None) -> Template:
    app = App()
    stack = LedgerAgentStack(
        app,
        "LedgerAgent",
        code=lambda_.Code.from_inline("def handler(event, context): pass"),
        ui_origins=UI_ORIGINS,
        smoke_test_user_id="agent-e2e-id",
        custom_domain=custom_domain,
        env=Environment(account="123456789012", region="ap-southeast-2"),
    )
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def template() -> Template:
    return synth(None)


@pytest.fixture(scope="module")
def with_domain() -> Template:
    return synth(DOMAIN)


def resources(template: Template, type_: str) -> Mapping[str, Mapping[str, Any]]:
    return template.find_resources(type_)


class TestTheUsageTable:
    def test_it_stays_inside_dynamodbs_always_free_capacity(self, template: Template) -> None:
        template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {"ProvisionedThroughput": {"ReadCapacityUnits": 1, "WriteCapacityUnits": 2}},
        )

    def test_it_expires_old_counts(self, template: Template) -> None:
        template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {"TimeToLiveSpecification": {"AttributeName": "expiresAt", "Enabled": True}},
        )


class TestTheFunction:
    def test_it_runs_python_3_13_on_arm(self, template: Template) -> None:
        template.has_resource_properties(
            "AWS::Lambda::Function",
            {
                "Runtime": "python3.13",
                "Architectures": ["arm64"],
                "Handler": "ledger_agent.lambda_function.handler",
                "Environment": {
                    "Variables": {
                        "LEDGER_MCP_URL": "https://api.haoran.cloud/api/mcp",
                        "USAGE_TABLE": Match.any_value(),
                        "SMOKE_TEST_USER_ID": "agent-e2e-id",
                    }
                },
            },
        )

    def test_it_times_out_before_the_http_apis_30_second_limit(self, template: Template) -> None:
        [function] = resources(template, "AWS::Lambda::Function").values()
        assert function["Properties"]["Timeout"] < 30

    def test_it_can_only_read_the_anthropic_api_key_and_update_the_usage_table(self, template: Template) -> None:
        [policy] = resources(template, "AWS::IAM::Policy").values()
        secret, usage = policy["Properties"]["PolicyDocument"]["Statement"]
        assert secret["Action"] == "ssm:GetParameter"
        assert "parameter/ledger-agent/anthropic-api-key" in str(secret["Resource"])
        assert usage["Action"] == "dynamodb:UpdateItem"
        assert "UsageTable" in str(usage["Resource"])

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


class TestWithoutACustomDomain:
    def test_there_is_no_certificate_domain_or_dns_record(self, template: Template) -> None:
        for type_ in (
            "AWS::CertificateManager::Certificate",
            "AWS::ApiGatewayV2::DomainName",
            "AWS::Route53::RecordSet",
        ):
            template.resource_count_is(type_, 0)


class TestWithACustomDomain:
    def test_a_free_dns_validated_certificate_is_issued_in_the_existing_zone(self, with_domain: Template) -> None:
        with_domain.has_resource_properties(
            "AWS::CertificateManager::Certificate",
            {
                "DomainName": "agent.haoran.cloud",
                "ValidationMethod": "DNS",
                "DomainValidationOptions": [{"DomainName": "agent.haoran.cloud", "HostedZoneId": "Z5UXKBB5IU578"}],
            },
        )

    def test_the_api_is_mapped_to_the_domain(self, with_domain: Template) -> None:
        with_domain.has_resource_properties("AWS::ApiGatewayV2::DomainName", {"DomainName": "agent.haoran.cloud"})
        with_domain.resource_count_is("AWS::ApiGatewayV2::ApiMapping", 1)

    def test_only_alias_records_for_the_agents_name_are_added_and_the_zone_is_not_created(
        self, with_domain: Template
    ) -> None:
        with_domain.resource_count_is("AWS::Route53::HostedZone", 0)
        records = resources(with_domain, "AWS::Route53::RecordSet").values()
        assert sorted(r["Properties"]["Type"] for r in records) == ["A", "AAAA"]
        for record in records:
            assert record["Properties"]["Name"] == "agent.haoran.cloud."
            assert record["Properties"]["HostedZoneId"] == "Z5UXKBB5IU578"
            assert "AliasTarget" in record["Properties"]

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from aws_cdk import Arn, ArnComponents, CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as targets
from aws_cdk.aws_apigatewayv2_integrations import HttpLambdaIntegration
from constructs import Construct

API_KEY_PARAMETER = "/ledger-agent/anthropic-api-key"
LEDGER_MCP_URL = "https://api.haoran.cloud/api/mcp"


@dataclass(frozen=True)
class CustomDomain:
    """A name in an existing Route 53 zone. The zone is imported by ID, never looked up (a lookup would put the
    account ID in cdk.context.json) and never modified beyond this name's records and ACM's validation record."""

    domain_name: str
    zone_name: str
    hosted_zone_id: str


class LedgerAgentStack(Stack):
    """The agent's Lambda behind an HTTP API. The API key parameter is created by hand, never here."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        code: lambda_.Code,
        ui_origins: Sequence[str],
        smoke_test_user_id: str,
        custom_domain: CustomDomain | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, id, **kwargs)

        # Provisioned, not on-demand: DynamoDB's always-free 25 units cover provisioned capacity only. Two writes a
        # second matches the stage's throttle. The counts are worth nothing after the day, so they aren't kept.
        usage = dynamodb.Table(
            self,
            "UsageTable",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PROVISIONED,
            read_capacity=1,
            write_capacity=2,
            time_to_live_attribute="expiresAt",
            removal_policy=RemovalPolicy.DESTROY,
        )

        function = lambda_.Function(
            self,
            "AgentFunction",
            code=code,
            handler="ledger_agent.lambda_function.handler",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            # More memory also means more CPU, which shortens the cold start's imports.
            memory_size=512,
            # The HTTP API gives up after 30 seconds; stop first so the error is ours.
            timeout=Duration.seconds(28),
            environment={
                "LEDGER_MCP_URL": LEDGER_MCP_URL,
                "USAGE_TABLE": usage.table_name,
                "SMOKE_TEST_USER_ID": smoke_test_user_id,
            },
            logging_format=lambda_.LoggingFormat.JSON,
            application_log_level_v2=lambda_.ApplicationLogLevel.INFO,
            log_group=logs.LogGroup(self, "AgentLogs", retention=logs.RetentionDays.TWO_WEEKS),
        )
        # SecureStrings under the default aws/ssm key need no KMS permission of their own.
        function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[
                    Arn.format(
                        ArnComponents(service="ssm", resource="parameter", resource_name=API_KEY_PARAMETER[1:]), self
                    )
                ],
            )
        )

        function.add_to_role_policy(iam.PolicyStatement(actions=["dynamodb:UpdateItem"], resources=[usage.table_arn]))

        api = apigw.HttpApi(
            self,
            "AgentApi",
            create_default_stage=False,
            cors_preflight=apigw.CorsPreflightOptions(
                allow_origins=list(ui_origins),
                allow_methods=[apigw.CorsHttpMethod.POST],
                allow_headers=["authorization", "content-type"],
                max_age=Duration.hours(1),
            ),
        )
        api.add_routes(
            path="/agent/v1/chat",
            methods=[apigw.HttpMethod.POST],
            # jsii's generated interfaces don't satisfy pyright's protocol check, though Function implements IFunction.
            integration=HttpLambdaIntegration("AgentIntegration", function),  # pyright: ignore[reportArgumentType]
        )
        domain = self._domain(custom_domain) if custom_domain else None
        # Every request that gets past the token check costs a Claude call, so keep the rate household-sized.
        stage = apigw.HttpStage(
            self,
            "DefaultStage",
            http_api=api,
            stage_name="$default",
            auto_deploy=True,
            throttle=apigw.ThrottleSettings(rate_limit=2, burst_limit=5),
            domain_mapping=apigw.DomainMappingOptions(domain_name=domain) if domain else None,
        )

        CfnOutput(self, "ApiUrl", value=stage.url)
        if custom_domain:
            CfnOutput(self, "CustomDomainUrl", value=f"https://{custom_domain.domain_name}/")

    def _domain(self, custom: CustomDomain) -> apigw.DomainName:
        """A regional custom domain: free certificate (an HTTP API, unlike CloudFront, takes one from its own
        region) and alias records, which Route 53 doesn't charge queries for."""
        zone = route53.HostedZone.from_hosted_zone_attributes(
            self, "Zone", hosted_zone_id=custom.hosted_zone_id, zone_name=custom.zone_name
        )
        certificate = acm.Certificate(
            self,
            "Certificate",
            domain_name=custom.domain_name,
            validation=acm.CertificateValidation.from_dns(zone),
        )
        domain = apigw.DomainName(self, "DomainName", domain_name=custom.domain_name, certificate=certificate)
        target = route53.RecordTarget.from_alias(
            # The same jsii/pyright protocol gap as HttpLambdaIntegration above.
            targets.ApiGatewayv2DomainProperties(  # pyright: ignore[reportArgumentType]
                domain.regional_domain_name, domain.regional_hosted_zone_id
            )
        )
        record_name = custom.domain_name.removesuffix(f".{custom.zone_name}")
        route53.ARecord(self, "AliasRecord", zone=zone, record_name=record_name, target=target)
        route53.AaaaRecord(self, "AliasRecordIpv6", zone=zone, record_name=record_name, target=target)
        return domain

from collections.abc import Sequence
from typing import Any

from aws_cdk import Arn, ArnComponents, CfnOutput, Duration, Stack
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk.aws_apigatewayv2_integrations import HttpLambdaIntegration
from constructs import Construct

API_KEY_PARAMETER = "/ledger-agent/anthropic-api-key"
LEDGER_MCP_URL = "https://api.haoran.cloud/api/mcp"


class LedgerAgentStack(Stack):
    """The agent's Lambda behind an HTTP API. The API key parameter is created by hand, never here."""

    def __init__(
        self, scope: Construct, id: str, *, code: lambda_.Code, ui_origins: Sequence[str], **kwargs: Any
    ) -> None:
        super().__init__(scope, id, **kwargs)

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
            environment={"LEDGER_MCP_URL": LEDGER_MCP_URL},
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
        # Every request that gets past the token check costs a Claude call, so keep the rate household-sized.
        stage = apigw.HttpStage(
            self,
            "DefaultStage",
            http_api=api,
            stage_name="$default",
            auto_deploy=True,
            throttle=apigw.ThrottleSettings(rate_limit=2, burst_limit=5),
        )

        CfnOutput(self, "ApiUrl", value=stage.url)

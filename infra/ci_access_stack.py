from typing import Any

from aws_cdk import Arn, ArnComponents, CfnOutput, Duration, Stack
from aws_cdk import aws_iam as iam
from constructs import Construct

GITHUB_OIDC = "token.actions.githubusercontent.com"
SMOKE_PASSWORD_PARAMETER = "/ledger-agent/e2e-password"


class CiAccessStack(Stack):
    """Deployed once from a laptop: lets GitHub Actions on main deploy LedgerAgent and run the smoke test, with no
    stored AWS keys. CI can't create the role it signs in with, which is why this is a separate stack.

    `github_subject_prefix` is the start of the repo's OIDC `sub` claim (immutable format), from
    `gh api repos/<owner>/<repo>/actions/oidc/customization/sub` (`sub_claim_prefix`).
    """

    def __init__(self, scope: Construct, id: str, *, github_subject_prefix: str, **kwargs: Any) -> None:
        super().__init__(scope, id, **kwargs)

        # The account's GitHub provider already exists (ledger-api's LedgerApiCiAccess stack owns it); an account can
        # have only one per URL, so refer to it by ARN rather than create or look it up.
        provider_arn = Arn.format(
            ArnComponents(service="iam", region="", resource="oidc-provider", resource_name=GITHUB_OIDC), self
        )
        role = iam.Role(
            self,
            "DeployRole",
            role_name="ledger-agent-github-deploy",
            max_session_duration=Duration.hours(1),
            # The same jsii/pyright protocol gap as in ledger_agent_stack.py.
            assumed_by=iam.WebIdentityPrincipal(  # pyright: ignore[reportArgumentType]
                provider_arn,
                {
                    "StringEquals": {
                        f"{GITHUB_OIDC}:aud": "sts.amazonaws.com",
                        f"{GITHUB_OIDC}:sub": f"{github_subject_prefix}:ref:refs/heads/main",
                    }
                },
            ),
        )
        # `cdk deploy` works by assuming the roles `cdk bootstrap` created, so this role needs nothing else to deploy.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["sts:AssumeRole"],
                resources=[
                    Arn.format(
                        ArnComponents(service="iam", region="", resource="role", resource_name="cdk-hnb659fds-*"), self
                    )
                ],
            )
        )
        # The smoke test signs in as its own ledger-api user; the user is already approved, so no admin password.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[
                    Arn.format(
                        ArnComponents(service="ssm", resource="parameter", resource_name=SMOKE_PASSWORD_PARAMETER[1:]),
                        self,
                    )
                ],
            )
        )

        CfnOutput(self, "DeployRoleArn", value=role.role_arn)

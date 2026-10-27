"""The LedgerAgentCiAccess stack: the role GitHub Actions on main assumes to deploy and run the smoke test."""

import os

os.environ.setdefault("JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION", "1")

import json
from typing import Any

import pytest
from aws_cdk import App, Environment
from aws_cdk.assertions import Template

from infra.ci_access_stack import CiAccessStack

SUBJECT_PREFIX = "repo:kingtigerwin@9473411/ledger-agent@1435332744"


@pytest.fixture(scope="module")
def template() -> Template:
    app = App()
    stack = CiAccessStack(
        app,
        "LedgerAgentCiAccess",
        github_subject_prefix=SUBJECT_PREFIX,
        env=Environment(account="123456789012", region="ap-southeast-2"),
    )
    return Template.from_stack(stack)


def role(template: Template) -> dict[str, Any]:
    [only] = template.find_resources("AWS::IAM::Role").values()
    return dict(only["Properties"])


def statements(template: Template) -> list[dict[str, Any]]:
    [policy] = template.find_resources("AWS::IAM::Policy").values()
    return list(policy["Properties"]["PolicyDocument"]["Statement"])


class TestTheOidcProvider:
    def test_it_reuses_the_accounts_existing_provider_instead_of_creating_one(self, template: Template) -> None:
        # An account can have only one provider for GitHub's URL, and ledger-api's CI-access stack owns it.
        template.resource_count_is("AWS::IAM::OIDCProvider", 0)
        template.resource_count_is("Custom::AWSCDKOpenIdConnectProvider", 0)
        principal = role(template)["AssumeRolePolicyDocument"]["Statement"][0]["Principal"]["Federated"]
        assert "oidc-provider/token.actions.githubusercontent.com" in json.dumps(principal)


class TestTheDeployRole:
    def test_only_this_repos_main_branch_can_assume_it(self, template: Template) -> None:
        properties = role(template)
        assert properties["RoleName"] == "ledger-agent-github-deploy"
        [trust] = properties["AssumeRolePolicyDocument"]["Statement"]
        assert trust["Action"] == "sts:AssumeRoleWithWebIdentity"
        assert trust["Condition"] == {
            "StringEquals": {
                "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                "token.actions.githubusercontent.com:sub": f"{SUBJECT_PREFIX}:ref:refs/heads/main",
            }
        }

    def test_it_can_only_assume_cdks_bootstrap_roles_and_read_the_smoke_test_password(self, template: Template) -> None:
        by_action = {s["Action"]: json.dumps(s["Resource"]) for s in statements(template)}
        assert set(by_action) == {"sts:AssumeRole", "ssm:GetParameter"}
        assert "role/cdk-hnb659fds-*" in by_action["sts:AssumeRole"]
        assert "parameter/ledger-agent/e2e-password" in by_action["ssm:GetParameter"]

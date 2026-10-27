import os
import sys
from pathlib import Path

# `cdk` runs this file as a script; make `infra` importable as a package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aws_cdk import App, Environment
from aws_cdk import aws_lambda as lambda_

from infra.ci_access_stack import CiAccessStack
from infra.ledger_agent_stack import CustomDomain, LedgerAgentStack
from infra.package import build_lambda_package

app = App()
ui_origins: list[str] = app.node.try_get_context("uiOrigins") or []
# {"domainName", "zoneName", "hostedZoneId"}, or "" for none.
domain: dict[str, str] | None = app.node.try_get_context("customDomain") or None
env = Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region="ap-southeast-2")
LedgerAgentStack(
    app,
    "LedgerAgent",
    code=lambda_.Code.from_asset(str(build_lambda_package())),
    ui_origins=ui_origins,
    custom_domain=CustomDomain(domain["domainName"], domain["zoneName"], domain["hostedZoneId"]) if domain else None,
    env=env,
)
# Deployed once by hand; CI then deploys only LedgerAgent.
CiAccessStack(app, "LedgerAgentCiAccess", github_subject_prefix=app.node.get_context("githubSubjectPrefix"), env=env)
app.synth()

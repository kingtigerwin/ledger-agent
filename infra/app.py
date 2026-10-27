import os
import sys
from pathlib import Path

# `cdk` runs this file as a script; make `infra` importable as a package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aws_cdk import App, Environment
from aws_cdk import aws_lambda as lambda_

from infra.ledger_agent_stack import LedgerAgentStack
from infra.package import build_lambda_package

app = App()
ui_origins: list[str] = app.node.try_get_context("uiOrigins") or []
LedgerAgentStack(
    app,
    "LedgerAgent",
    code=lambda_.Code.from_asset(str(build_lambda_package())),
    ui_origins=ui_origins,
    env=Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region="ap-southeast-2"),
)
app.synth()

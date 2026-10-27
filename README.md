# ledger-agent

Turns a customer's free text ("I went to starbucks and bought coffee spending 5 dollars") into a transaction in [ledger-api](https://github.com/kingtigerwin/ledger-api), using Claude over ledger-api's MCP endpoint as that customer. Python on AWS Lambda behind an HTTP API at `https://agent.haoran.cloud`. The design is in [`docs/design.md`](docs/design.md).

```sh
curl -s https://agent.haoran.cloud/agent/v1/chat \
  -H "authorization: Bearer $LEDGER_API_ACCESS_TOKEN" -H 'content-type: application/json' \
  -d '{"messages":[{"role":"user","content":"I went to starbucks and bought coffee spending 5 dollars"}],"timeZone":"Australia/Sydney"}'
```

## Develop

```sh
uv sync
uv run ruff check && uv run ruff format --check && uv run pyright && uv run pytest
```

## First deploy (once, from your machine)

GitHub Actions deploys `main` using GitHub's OIDC login, so no AWS keys are stored in GitHub. It can't create the role it signs in with, so that role is deployed once by hand. It reuses the account's GitHub OIDC provider, which ledger-api's `LedgerApiCiAccess` stack created.

1. With AWS credentials for the account in your shell (ap-southeast-2 is already CDK-bootstrapped by ledger-api), and these SSM `SecureString`s, made by hand:
   - `/ledger-agent/anthropic-api-key`: the Anthropic API key.
   - `/ledger-agent/e2e-password`: any long random string, e.g. `openssl rand -base64 24`; the smoke test's ledger-api user `agent-e2e` uses it.

   ```sh
   export JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1
   npx aws-cdk@2.1146.1 deploy LedgerAgent LedgerAgentCiAccess
   uv run python scripts/smoke.py   # first run registers agent-e2e and approves it with ledger-api's admin password
   ```

   Note the `DeployRoleArn` output.

2. In the GitHub repo's **Settings → Secrets and variables → Actions**, add:

   | Kind | Name | Value |
   |---|---|---|
   | Secret | `AWS_DEPLOY_ROLE_ARN` | the `DeployRoleArn` output (a secret, so the account ID in it stays out of the public logs) |
   | Variable | `DEPLOY_ENABLED` | `true` (turns the deploy job on) |

3. Push to `main`, or run the CI workflow on `main` from the Actions tab (**Run workflow**). The `deploy` job runs `cdk deploy LedgerAgent` and then the smoke test against `https://agent.haoran.cloud`.

## Cost

AWS stays within the free tier: Lambda, HTTP API, two SSM standard parameters, two-week log retention, a free ACM certificate and Route 53 alias records (the `haoran.cloud` zone already existed). Claude calls are the only paid part: Haiku 5.5, bounded by the token check before any call, stage throttling (2 requests per second), request size limits and at most 4 Claude calls per turn. Each deploy's smoke test makes one call.

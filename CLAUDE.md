# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Python 3.13 (pinned in `.python-version`, matching the Lambda runtime), managed with `uv`.

```sh
uv sync                                   # install runtime, dev and infra (CDK) dependencies into .venv
uv run pytest                             # all tests
uv run pytest tests/unit/test_clock.py    # one file
uv run pytest -k "sydney"                 # tests matching a name
uv run ruff check && uv run ruff format --check
uv run pyright                            # strict mode

# One real turn: real Claude (costs money) and the deployed ledger-api; reads the API key from SSM with your AWS creds.
LEDGER_ACCESS_TOKEN=... uv run python -m ledger_agent "I went to starbucks and bought coffee spending 5 dollars"

# CDK (same CLI version as ledger-api); synth builds the Lambda package into build/lambda first.
export JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1   # jsii warns about the local Node 26
npx aws-cdk@2.1146.1 synth
npx aws-cdk@2.1146.1 deploy LedgerAgent
```

CI (`.github/workflows/ci.yml`) runs the same four checks on pull requests and pushes to `main`. No test calls Claude or the network; `tests/infra/` synthesizes the stack with inline placeholder code, so it never builds the package.

## Architecture

- `agent.py`: `run_turn` is the whole agent: token check via `tools/list`, the Claude tool-use loop, tool filtering, and collecting created transactions. It depends only on two protocols, `Model` (real one: `Claude`, wrapping `AsyncAnthropic`) and `Ledger` (`ledger.py`), so `tests/unit/` drives it with the fakes in `tests/unit/fakes.py`.
- `mcp_ledger.py`: the real `Ledger`, one MCP session per turn (`connect(url, token)`). The MCP SDK reports every HTTP error the same way, so an httpx2 response hook records status codes and `_guard` maps 401/403 to `Unauthorized` and everything else to `LedgerUnavailable`.
- `handler.py`: `create_handler(Deps)` turns an HTTP API payload-v2 event into a turn (bearer token, pydantic request validation, size limits) and maps failures to `{ error: { code, message } }`: 401 `unauthorized`, 400 `invalid_json` / `validation_failed` / `invalid_time_zone`, 502 `ledger_unavailable`, 503 `model_unavailable`, 500 `internal_error`. `lambda_function.py` is the thin Lambda entry that wires real dependencies once per cold start; it keeps one event loop for the container because the Anthropic client's connection pool is bound to the loop that created it.
- `infra/`: CDK app (`app.py`), the `LedgerAgent` stack (`ledger_agent_stack.py`: Lambda, HTTP API with CORS from the `uiOrigins` context in `cdk.json`, stage throttling, read access to the SSM key only), and `package.py`, which builds the Lambda folder with `uv export` + `uv pip install --python-platform aarch64-manylinux2014` (no Docker), leaving out boto3/botocore since the runtime provides them.
- `tests/integration/` runs `McpLedger` against the MCP SDK's own server in process (`httpx2.ASGITransport`), behind a fake authorizer. Open the server per test with `served()`, not a pytest fixture: its lifespan must exit in the task that entered it.
- The Anthropic SDK is 2.x: `MessageParam` is a union, so build `UserMessageParam` / `AssistantMessageParam`; it uses `httpx2`, not `httpx`. `mypy_boto3_ssm` is a dev-only stub package, so import it under `TYPE_CHECKING` only.

## What this is

A Python agent on AWS Lambda that turns a customer's free text into ledger-api transactions using Claude (`claude-haiku-5-5`), recording them immediately through ledger-api's MCP endpoint (`POST /api/mcp`) with the customer's own access token. The React UI calls its HTTP API. ledger-api lives in the sibling repo `../ledger-api`; its `docs/design.md` describes the MCP tools and the token/scope model this agent depends on.

## Design and working agreement

`docs/design.md` is the agreed design: the record-immediately flow, API shape, auth, prompt rules and cost limits. Read it before adding features, and update it when a design decision changes.

- Claude gets only `create_transaction`, `list_transactions` and `get_monthly_summary` from the MCP server; filter out the rest, so it can't update or delete existing transactions.
- Never retry an MCP tool call: `create_transaction` isn't idempotent, so a retry can record an expense twice.
- Check the token (MCP `tools/list`) before any Claude call, so unauthenticated requests cost nothing.
- The Anthropic API key lives only in SSM (`/ledger-agent/anthropic-api-key`, ap-southeast-2). Never log the customer's token or the API key.
- Keep AWS at $0/month (free-tier services only); Claude calls are the only paid part, so keep them bounded and call out anything else that costs money.
- Work test-first, like ledger-api: failing test, make it pass, refactor. Name tests BDD-style: `class TestGiven…` / `def test_it_…`.
- Pass time in (`now: datetime`) rather than reading the clock inside logic, so tests are deterministic.

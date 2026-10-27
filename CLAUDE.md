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
```

CI (`.github/workflows/ci.yml`) runs the same four checks on pull requests and pushes to `main`. No test calls Claude or the network.

## Architecture

- `agent.py`: `run_turn` is the whole agent: token check via `tools/list`, the Claude tool-use loop, tool filtering, and collecting created transactions. It depends only on two protocols, `Model` (real one: `Claude`, wrapping `AsyncAnthropic`) and `Ledger` (`ledger.py`), so `tests/unit/` drives it with the fakes in `tests/unit/fakes.py`.
- `mcp_ledger.py`: the real `Ledger`, one MCP session per turn (`connect(url, token)`). The MCP SDK reports every HTTP error the same way, so an httpx2 response hook records status codes and `_guard` maps 401/403 to `Unauthorized` and everything else to `LedgerUnavailable`.
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

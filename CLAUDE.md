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
```

CI (`.github/workflows/ci.yml`) runs the same four checks on pull requests and pushes to `main`.

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

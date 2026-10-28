# ledger-agent design

An agent that turns a customer's free text ("I went to starbucks and bought coffee spending 5 dollars") into transactions in [ledger-api](../../ledger-api), by calling ledger-api's MCP endpoint as that customer. The React UI calls this agent's API. Python, Claude (Anthropic API), AWS Lambda.

## Stack

- Python 3.13 on Lambda (arm64), API Gateway HTTP API in front, deployed with AWS CDK (Python) as one stack, `LedgerAgent`, in `ap-southeast-2` (same region and account as ledger-api).
- Custom domain `https://agent.haoran.cloud` (configured in `cdk.json`): a free regional ACM certificate (an HTTP API takes one from its own region, so no `us-east-1` stack as ledger-api's CloudFront needs) and `A`/`AAAA` alias records in the existing `haoran.cloud` zone, which is imported by ID and otherwise left alone. Alias queries to API Gateway are free. The `execute-api` URL keeps working.
- Claude via the Anthropic Python SDK, model `claude-haiku-5-5` (chosen for cost), `output_config.effort: "low"`, low `max_tokens`. The model is one config value.
- The Anthropic API key is the SSM `SecureString` `/ledger-agent/anthropic-api-key`, created by hand (never by CDK, never in source). The Lambda reads it once per cold start.
- ledger-api's MCP endpoint, `POST https://api.haoran.cloud/api/mcp` (stateless Streamable HTTP, JSON responses), called with the Python `mcp` SDK's client.

## Auth

- The UI signs the customer in to ledger-api over REST and sends their ledger-api access token to this API as `Authorization: Bearer …`.
- The agent never verifies the JWT itself. Its first call on every request is MCP `tools/list` with that token, which ledger-api's authorizer checks; a 401/403 there becomes a 401 from the agent **before any Claude call**, so unauthenticated callers cost nothing.
- Every MCP call uses the customer's own token, so the agent can only ever read or write that customer's transactions with that token's scopes. The token is never logged and never sent to Anthropic.

## Flow: record immediately

`POST /agent/v1/chat` runs a Claude tool-use loop. Claude is given three of ledger-api's MCP tools (four for an admin), with the names, descriptions and input schemas `tools/list` returns, and each call is forwarded over MCP with the customer's token:

- `create_transaction` — records an expense straight away; there is no confirmation step.
- `list_transactions`, `get_monthly_summary` — read-only, for questions about past spending.
- `list_all_transactions` — admins only, read-only: every customer's transactions (or one customer's, by `username`), each with its `userId` and `username`. ledger-api lists it only for tokens with the admin role and `admin:read`, so the agent offers it whenever `tools/list` returns it and never decides who is an admin itself. Only then does the prompt get an admin paragraph, so customers' prompts are unchanged.

`get_transaction`, `update_transaction` and `delete_transaction` are filtered out, so Claude can't change or delete existing transactions, even an admin's; ledger-api has no admin edit or delete tools over MCP either. New expenses are always recorded in the signed-in customer's own ledger. The customer fixes a wrong record in the UI over REST.

The loop stops when Claude ends its turn or after a fixed number of tool rounds. The response carries Claude's reply and, as data, everything the tools returned during the turn: the transactions `create_transaction` recorded (so the UI can show them and refresh its list), the transactions `list_transactions` and `list_all_transactions` returned, each once even across pages, and every `get_monthly_summary` result. Because the UI shows the lookups as a table, Claude keeps its reply to a sentence or two instead of writing the list out, which also keeps long months inside its output limit.

Chat is for what the customer types: recording and questions about their own spending ("how much have I spent this month?", "show all my expenses this month"), and for an admin, everyone's ("show haoran's spending this month"). The app's own screens (the transaction table, edit and delete, admin pages) call ledger-api directly: they need exact data and no Claude call. Customers can't ask about anyone else's spending: their tokens never see `list_all_transactions`.

`create_transaction` isn't idempotent (ledger-api makes the id), so:

- the agent never retries a tool call; a failed call goes back to Claude as a tool error, and the prompt tells Claude not to call `create_transaction` again for the same expense but to tell the customer it may not have been saved;
- the UI should disable Send while a request is in flight.

If duplicates become a problem, the fix is in ledger-api: let `create_transaction` accept an optional client ULID `id`, as the REST create route already does.

## API

All under `/agent/v1`, all requiring the bearer token.

`POST /chat`
```json
{
  "messages": [{ "role": "user", "content": "I went to starbucks and bought coffee spending 5 dollars" }],
  "timeZone": "Australia/Sydney"
}
```
→ 200
```json
{
  "reply": "Recorded $5.00 at Starbucks for coffee (dining) today.",
  "transactions": [
    {
      "id": "01JBX...", "date": "2026-10-27", "merchant": "Starbucks", "category": "dining", "item": "coffee",
      "amountCents": 500, "currency": "AUD", "createdAt": "...", "updatedAt": "...", "version": 1
    }
  ],
  "listed": [],
  "summaries": []
}
```

For "how much have I spent this month?", `transactions` and `listed` are empty and `summaries` holds ledger-api's summary: `[{ "month": "2026-10", "currency": "AUD", "totalCents": 12290, "byCategory": [{ "category": "groceries", "totalCents": 8240 }, ...] }]`. For "show all my expenses this month", `listed` holds the month's transactions, newest first, in the same shape as `transactions`; from `list_all_transactions` each item also has `userId` and `username` (`null` if that user can't be found), so the UI can show whose it is.

- No server-side conversation state: the UI sends the whole history (user and assistant text only) every turn and appends the reply. Limits on message count and length keep requests, and Claude spend, bounded.
- `timeZone` is an IANA name; the agent puts today's date in that zone into the prompt so "today" and "yesterday" resolve to real dates.

Errors follow ledger-api's convention: `{ "error": { "code", "message" } }`.

## Prompt rules

- "Dollars" means AUD, the only currency ledger-api accepts. If the customer names another currency, say so instead of converting.
- If the amount or what was bought is missing, ask instead of recording; never guess an amount.
- One `create_transaction` call per expense; several expenses in one message give several calls.
- Pick the closest of ledger-api's fixed categories, else `other`. Merchant names are written in their usual form ("Starbucks").
- After recording, confirm what was saved (amount, merchant, item, category, date) in one short sentence.

## Limits and cost

- The AWS side stays within the free tier (Lambda, HTTP API, one SSM standard parameter). **Claude calls are the one thing that costs money**; they are bounded by: rejecting bad tokens before calling Claude, HTTP API stage throttling, request size limits, a cap on tool-use rounds per turn, and a low `max_tokens`.
- HTTP API's 30-second integration timeout bounds each turn (the Lambda stops at 28 seconds); a turn is typically one or two Claude calls and at most a couple of MCP calls, and at most 4 Claude calls.
- Requests: at most 20 messages of at most 2,000 characters each, and the last one must be the customer's. The stage is throttled to 2 requests per second with bursts of 5.
- If Claude fails after something was recorded, the turn still answers 200 with what was recorded and a "couldn't finish" reply, so the customer hears about it; if nothing was recorded, it answers 503.
- CORS allows only the UI origins listed in `cdk.json` (`uiOrigins`), with the `authorization` and `content-type` headers.

## Out of scope for now

Streaming replies, updating or deleting transactions through the agent, server-side conversation history, Bedrock, the UI itself.

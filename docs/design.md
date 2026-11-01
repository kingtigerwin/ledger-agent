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

## Flow: reading a receipt

`POST /agent/v1/receipts/read` reads a photo of a receipt. It's one Claude call with structured output and no tools, so text in a photo can't make anything happen. It saves nothing: the UI records the result with ledger-api's `POST /transactions` and an ID it generates, so a retry can't record it twice, which `create_transaction` can't promise. The photo is never stored or logged, and goes no further than Claude; the customer's photos going to Anthropic is the one privacy cost of this. Claude is told not to return card numbers, loyalty numbers or staff names.

Claude copies what's printed and code does the rest (`receipts.py`): it parses the date day first (these are Australian receipts, so 01/11/2026 is 1 November), and does the sums, since a misread amount rarely leaves both the lines and the payments matching the total. Each failed check adds a code to `problems`, and `canRecord` is true only when there are none. The UI records a receipt that can be recorded, and shows the customer a form for one that can't.

| Problem | Means |
|---|---|
| `merchant_unreadable` | No merchant name |
| `date_unreadable`, `date_in_future`, `date_too_old` | No valid date, a date after today in the customer's time zone, or one over a year old |
| `lines_dont_add_up` | The lines don't sum to the total |
| `payments_dont_add_up` | The payments don't sum to the total, so `paidCents` falls back to the total |
| `nothing_paid` | It was all paid with points, so there's no money to record (ledger-api needs an amount above zero) |

`paidCents` is the money paid, and it's what the transaction records, so spend matches the customer's bank; `totalCents` is the goods' value. A $30.50 shop with $30.00 of Flybuys points records $0.50. Each line has `name` as printed and `product` in plain words (`CLS CHKN DRUMSTICKS` is `chicken drumsticks`), for a future search of what was bought; photos aren't kept, so that can't be added afterwards from the photo.

The four sample receipts in the design discussion were all read correctly on `claude-haiku-5-5` at `low` effort, at $0.0004 to $0.0011 each and 2 to 7 seconds. That's only four fairly clear photos, which is why the checks decide and Claude doesn't.

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

`POST /receipts/read`
```json
{ "image": "<base64>", "mediaType": "image/jpeg", "timeZone": "Australia/Melbourne" }
```
→ 200
```json
{
  "receipt": {
    "merchant": "Coles", "date": "2026-11-01", "category": "groceries", "paidCents": 50, "totalCents": 3050,
    "gstCents": 0, "savingsCents": 3100, "time": "15:06", "location": "The Glen", "receiptNumber": "6547",
    "abn": "45004189708",
    "lines": [{ "name": "CLS CHICKEN MINCE", "product": "chicken mince", "amountCents": 650 }],
    "payments": [{ "label": "EFT", "kind": "money", "amountCents": 50 }]
  },
  "problems": [],
  "canRecord": true
}
```
It answers 413 `image_too_large` over about 1.1 MB, and 422 `not_a_receipt`, `unreadable` or `receipt_too_long`, each with a message for the customer. PDFs aren't accepted.

Errors follow ledger-api's convention: `{ "error": { "code", "message" } }`. A customer past their daily limit gets 429 `daily_limit_reached`, and past the total for everyone, 429 `daily_total_reached`.

## Prompt rules

- "Dollars" means AUD, the only currency ledger-api accepts. If the customer names another currency, say so instead of converting.
- If the amount or what was bought is missing, ask instead of recording; never guess an amount.
- One `create_transaction` call per expense; several expenses in one message give several calls.
- Pick the closest of ledger-api's fixed categories, else `other`. Merchant names are written in their usual form ("Starbucks").
- After recording, confirm what was saved (amount, merchant, item, category, date) in one short sentence.

## Limits and cost

- The AWS side stays within the free tier (Lambda, HTTP API, one SSM standard parameter). **Claude calls are the one thing that costs money**; they are bounded by: rejecting bad tokens before calling Claude, a daily limit per customer and a daily total across everyone, HTTP API stage throttling, request size limits, a cap on tool-use rounds per turn, and a low `max_tokens`.
- Each customer also gets 10 receipts a day, counted separately from messages, since one shop can leave several and each is a single small call; each receipt also counts towards the daily total. A receipt is counted after the token check and before Claude, so one that can't be read still counts.
- Claude's output for a receipt is capped at 4,500 tokens, about 50 lines: more wouldn't finish inside the Lambda's 28 seconds. A longer one gets 422 `receipt_too_long`.
- Each customer, admins included, gets 5 messages a day, reset at midnight Sydney time (one zone for everyone, since the UI supplies the customer's own, and changing it could buy extra days). A message is counted straight after the token check and before any Claude call, whatever happens to it next, with one conditional write to the `Usage` table, so messages sent together can't pass the limit and a failure to count stops the turn. Past the limit the agent answers 429 `daily_limit_reached` and calls nothing. The user comes from the token's `sub`, read only once ledger-api has accepted that token. Everyone together gets 300 messages a day, for the same day: anyone can join with a Google account, so this is what many accounts together run into. Past it the agent answers 429 `daily_total_reached`, and the warning log `daily_total_reached` marks the day. It is counted after the customer's own limit, so a customer already past theirs can't use it up with refused messages; a message refused by the total still counts towards the customer's own limit. The deploy smoke test's `agent-e2e` user (`smokeTestUserId` in `cdk.json`) is exempt: it sends two messages per deploy, so a third deploy in a day would fail its smoke test, and it isn't counted towards the total either. Items expire through DynamoDB TTL. The table is provisioned at 1 read and 4 writes a second, inside the always-free 25 units; on-demand isn't covered by them.
- HTTP API's 30-second integration timeout bounds each turn (the Lambda stops at 28 seconds); a turn is typically one or two Claude calls and at most a couple of MCP calls, and at most 4 Claude calls.
- Requests: at most 20 messages of at most 2,000 characters each, and the last one must be the customer's. The stage is throttled to 2 requests per second with bursts of 5.
- If Claude fails after something was recorded, the turn still answers 200 with what was recorded and a "couldn't finish" reply, so the customer hears about it; if nothing was recorded, it answers 503.
- CORS allows only the UI origins listed in `cdk.json` (`uiOrigins`), with the `authorization` and `content-type` headers.

## Out of scope for now

Streaming replies, updating or deleting transactions through the agent, server-side conversation history, Bedrock, the UI itself.

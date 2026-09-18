<p align="center">
  <img src="assets/icon.png" alt="WFM Advisor Icon" width="96" height="96" />
</p>

<h1 align="center">WFM Advisor</h1>

<p align="center">
  A Discord bot and CLI tool that helps Warframe players decide when to sell or hold Prime items. It analyzes Warframe Market historical price trends, relic vaulting cycles, and recent patch balance notes to recommend trade actions, evaluate fair prices, and optimize inventory value.
</p>

<p align="center">
  <a href="https://discord.com/oauth2/authorize?client_id=1549213121791393852&scope=bot%20applications.commands&permissions=2147551232"><img src="https://img.shields.io/badge/Discord-Add%20Bot%20to%20Server-5865F2?style=for-the-badge&logo=discord&logoColor=white" alt="Discord Bot" /></a>
  <a href="https://aws.amazon.com/lambda/"><img src="https://img.shields.io/badge/AWS-Lambda%20Serverless-FF9900?style=for-the-badge&logo=amazon-aws&logoColor=white" alt="AWS Serverless" /></a>
  <a href="https://deepmind.google/technologies/gemini/"><img src="https://img.shields.io/badge/Model-Gemini%203.5%20Flash%20Lite-4285F4?style=for-the-badge&logo=google&logoColor=white" alt="Model" /></a>
</p>


> **Disclaimer**: These recommendations are synthesized from statistical price trends, relic vault schedules, and game patch notes to inform your trades on Warframe Market. They are not guaranteed market forecasts.

---

## Discord Bot

You can add the hosted bot to your Discord server:

👉 **[Invite WFM Advisor to your Discord server](https://discord.com/oauth2/authorize?client_id=1549213121791393852&scope=bot%20applications.commands&permissions=2147551232)**

### Supported Slash Commands

| Command | Description | Example |
|---|---|---|
| `/ask <query>` | Ask freeform market questions to the conversational agent | `/ask Is 45p fair for Rhino Prime BP?` |
| `/card <item>` | Full SELL/HOLD recommendation card with signal breakdown and reasoning | `/card Rhino Prime` |
| `/fair <item> <price>` | Compares an offered platinum price against the 48-hour volume-weighted median | `/fair Rhino Prime Blueprint 45` |
| `/parts <item>` | Set price vs. component sum breakdown, taking trade slot limits into account | `/parts Saryn Prime` |
| `/vault <item>` | Vault status, days vaulted, and Prime Resurgence rotation schedule | `/vault Volt Prime` |

---

## Demo

![WFM Advisor Discord Bot Demo](assets/discord_demo.gif)

---

## Architecture & How It Works

### The Tool-Calling Agent (`nodes/agent.py`, `nodes/tools.py`)

The conversational layer uses LangGraph's `StateGraph(MessagesState)` with prebuilt `ToolNode` and `tools_condition`, backed by `gemini-3.5-flash-lite`. When an open-ended question comes in, the model inspects the query, invokes one or more domain tools to gather market context, and synthesizes a final recommendation.

The agent has 6 domain tools available:
- `get_price_trend`: Fits a linear regression over 90 days of daily order statistics, returning slope, $R^2$, and percentage change over the window.
- `get_vault_status`: Calculates how long an item has been vaulted and checks active Prime Resurgence rotation dates.
- `get_patch_impact`: Reads patch notes from the past 90 days and filters for genuine frame buffs and nerfs, ignoring cosmetic changes and unrelated bug fixes.
- `check_fair_price`: Compares an offered trade price against the 48-hour volume-weighted median, labeling the price as **Bargain**, **Fair**, or **Overpriced**.
- `compare_set_vs_parts`: Evaluates full set market price against the sum of individual component pieces, factoring in the daily trade slot cost of selling multiple parts.
- `get_market_signals`: Fetches trend, vault status, and patch signals in one call for full BUY/SELL/HOLD evaluations.

### Dual-Path Query Routing (`ingest/cache_manager.py`)

Not every lookup needs an LLM reasoning loop. The query dispatcher in `get_recommendation()` splits incoming traffic into two distinct paths based on query shape:

1. **Fast deterministic pipeline**: If the query is a simple item name like `"rhino prime"` or `"wisp prime sys"`, it bypasses the agent entirely. It runs through slug resolution, checks the local SQLite cache for staleness, computes the statistical trend, pulls vault and patch data, and generates a formatted card. This keeps basic price card lookups fast and predictable.
2. **Conversational agent**: If the query contains intent keywords (`"fair"`, `"vault"`, `"parts"`, `"buy"`, `"compare"`, etc.) or exceeds 4 words, the router forwards the request to `run_market_agent()`. This allows the agent to chain multiple tool calls, handle comparisons between two items, or answer specific questions about margins and trade fairness.

### Context Bounds & Token Management

Longer conversations and verbose patch logs can quickly inflate token counts and trigger API errors if unmanaged. Two safeguards keep context predictable:

- **Sliding-window message trimming**: The agent uses LangChain's `trim_messages` configured with `token_counter=len` and `max_tokens=10`. This keeps conversation history capped at the last 10 messages while keeping the trading system prompt pinned at index 0. Setting `start_on="human"` and `allow_partial=False` ensures older turns drop off cleanly without severing tool calls from their corresponding tool results, which would otherwise cause API validation failures.
- **Patch history capping**: `compute_patch_signal` in `nodes/patch_node.py` extracts lines that specifically mention the frame or its abilities and evaluates at most the 8 most recent patch entries from the 90-day window, preventing lengthy patch logs from bloating the prompt.

### AWS Lambda & Serverless Infrastructure (`lambda_handler.py`)

The application is deployed to AWS Lambda behind a public Function URL. Because monthly query volume easily fits inside Lambda's Always Free tier (1M requests and 400,000 GB-seconds per month), hosting cost is effectively zero at this scale.

#### Discord's 3-Second Timeout & Async Self-Invocation
Discord's HTTP interaction API requires a valid response within 3 seconds, or the interaction token expires and the user sees an error. Because an LLM agent call with multiple tool executions often takes 4–8 seconds, `lambda_handler.py` handles slash commands with an asynchronous self-invocation workflow:

1. Lambda receives the interaction payload and validates the `X-Signature-Ed25519` and `X-Signature-Timestamp` headers using `pynacl`.
2. It returns an immediate deferred ACK (`{"type": 5}`) within ~150ms, which tells Discord to display the bot's "Thinking..." state.
3. Before finishing that initial request, Lambda invokes itself asynchronously via `boto3` (`InvocationType="Event"`), passing along the parsed command and token in a worker payload (`{"discord_worker": True, ...}`).
4. The asynchronous worker execution loads credentials from SSM Parameter Store, runs the tool or agent query, builds the Discord embed, and updates the original deferred message via a `PATCH` request to Discord's webhook URL (`/webhooks/{application_id}/{token}/messages/@original`).

#### S3 Database Cache & Scheduled Monitoring
SQLite database state (`wfm.db`) is preserved across cold starts using Amazon S3 (`s3://wfm-advisor-cache/wfm.db`). On cold start, Lambda downloads the database if `/tmp/wfm.db` does not exist (falling back to a bundled seed database on initial setup). Whenever an on-demand query or background worker writes fresh data, it uploads the updated database back to S3.

An Amazon EventBridge rule triggers batch scans daily at 02:00 UTC. The batch job scans items in `config/watchlist.json`, checks configured price thresholds, and posts an alert to a Discord webhook if a SELL signal is identified or if an active Prime Resurgence rotation is ending in 7 days or fewer.

---

## Quickstart

### 1. Installation
Clone the repository and set up a Python virtual environment:
```bash
git clone https://github.com/mnarla/wfmarket-advisor.git
cd wfmarket-advisor
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Environment Configuration
Create a `.env` file in the project root:
```env
# LLM Providers
GEMINI_API_KEY=your_gemini_api_key_here

# Discord Bot Credentials
DISCORD_APPLICATION_ID=your_application_id_here
DISCORD_PUBLIC_KEY=your_public_key_hex_here
DISCORD_BOT_TOKEN=your_bot_token_here
DISCORD_WEBHOOK_URL=your_discord_webhook_url_here  # For batch channel alerts
```

### 3. Usage

You can query items directly from the CLI or run an interactive prompt session.

Interactive shell:
```bash
python main.py -i
```

One-shot lookups (fast pipeline path):
```bash
python main.py -q "rhino prime"
python main.py -q "soma prime"
```

Conversational queries (routed to the LangGraph agent):
```bash
python main.py -q "Is 45p fair for Rhino Prime Blueprint?"
python main.py -q "Sell Saryn Prime as set or parts?"
python main.py -q "Is Volt Prime vaulted?"
python main.py -q "Compare Rhino Prime vs Saryn Prime"
```

Register Discord slash commands:
```bash
python -m discord_bot.register_commands
```

Deploy to AWS Lambda:
```bash
bash scripts/deploy_aws.sh
```

---

## Running Tests

Run the test suite with pytest:
```bash
pytest
```

The 72 automated tests cover item slug resolution, cache staleness checks, domain market tools, conversational routing, Discord Ed25519 signature validation, and Lambda execution paths (including the asynchronous worker dispatch and EventBridge batch processing).

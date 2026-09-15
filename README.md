# WFM Sell-Timing Advisor

An autonomous, multi-signal market intelligence engine and serverless Discord bot for **Warframe Market** built using **LangGraph**, **Gemini 3.5 Flash Lite**, **SQLite**, and **AWS Lambda**.

[![Discord Bot](https://img.shields.io/badge/Discord-Add%20Bot%20to%20Server-5865F2?style=for-the-badge&logo=discord&logoColor=white)](https://discord.com/oauth2/authorize?client_id=1549213121791393852&scope=bot%20applications.commands&permissions=2147551232)
[![AWS Serverless](https://img.shields.io/badge/AWS-Lambda%20Serverless-FF9900?style=for-the-badge&logo=amazon-aws&logoColor=white)](https://aws.amazon.com/lambda/)
[![Model](https://img.shields.io/badge/Model-Gemini%203.5%20Flash%20Lite-4285F4?style=for-the-badge&logo=google&logoColor=white)](https://deepmind.google/technologies/gemini/)
[![Tests](https://img.shields.io/badge/Tests-72%20Passing-brightgreen?style=for-the-badge)](tests/)

> **Disclaimer**: These outputs are not guaranteed financial predictions. They are data-driven recommendations synthesized from statistical price trends, relic vault supply cycles, and game patch balance notes to help inform your trading decisions on Warframe Market.

---

## Discord Bot

You can add the hosted, serverless bot directly to your Discord server:

👉 **[Click here to invite WFM Advisor to your Discord server](https://discord.com/oauth2/authorize?client_id=1549213121791393852&scope=bot%20applications.commands&permissions=2147551232)**

### Supported Slash Commands

| Command | Description | Example |
|---|---|---|
| `/ask <query>` | Ask any natural-language question to the autonomous market agent | `/ask Is 45p fair for Rhino Prime BP?` |
| `/card <item>` | Full SELL/HOLD recommendation card with signals and reasoning | `/card Rhino Prime` |
| `/fair <item> <price>` | Instant fair-price calculation against the 48-hour market median | `/fair Rhino Prime Blueprint 45` |
| `/parts <item>` | Full set vs. component parts margin breakdown and trade slot advice | `/parts Saryn Prime` |
| `/vault <item>` | Vault status, days vaulted, and live Prime Resurgence countdown | `/vault Volt Prime` |

---

## Demo

![WFM Advisor Discord Bot Demo](assets/discord_demo.gif)

---

## Architecture & Capabilities

### 1. Autonomous Tool-Calling Agent ([`nodes/agent.py`](nodes/agent.py), [`nodes/tools.py`](nodes/tools.py))
- Powered by **`gemini-3.5-flash-lite`** using LangGraph `StateGraph(MessagesState)` with prebuilt `ToolNode` and `tools_condition`.
- Equips the LLM with 6 domain tools:
  - `check_fair_price`: Compares an offered trade price against the 48-hour volume-weighted median, classifying trades as **Bargain**, **Fair**, or **Overpriced**.
  - `compare_set_vs_parts`: Evaluates full set market price against the sum of individual component parts, factoring in trade slot conservation.
  - `get_vault_status`: Calculates precise vault duration and active Prime Resurgence rotation schedules.
  - `get_price_trend`: Linear regression over 90-day price history ($R^2$, slope, percentage change).
  - `get_patch_impact`: Filters patch notes to evaluate genuine gameplay buffs and nerfs vs. cosmetic fixes.
  - `get_market_signals`: Fetches all signals simultaneously for holistic buy/sell advice.

### 2. Dual-Path Routing Engine ([`ingest/cache_manager.py`](ingest/cache_manager.py))
- **Fast Pipeline Path**: Bare item queries (e.g. `"rhino prime"`, `"wisp prime sys"`) bypass agent loops entirely and run the instant deterministic sequential pipeline.
- **Conversational Path**: Questions with intent keywords (`"fair"`, `"vault"`, `"parts"`, `"buy"`, `"compare"`) dynamically route to the tool-calling agent.

### 3. Context Window Management & Guardrails
- **Sliding Window Message Trimming**: Employs LangChain's `trim_messages` to maintain the trading system prompt at index 0 while bounding conversation history to the last 10 turns. Prevents context rot and ensures tool-call / tool-result message alignment.
- **Token Capping**: Automatically caps patch history analysis to the 8 most recent entries to prevent prompt bloat.

### 4. 100% Serverless AWS Lambda Deployment ([`lambda_handler.py`](lambda_handler.py))
- **Zero Idle Cost**: Runs entirely on AWS Lambda via public Function URLs ($0.00 perpetual free tier).
- **Discord HTTP Interactions**: Handles incoming Discord interactions with Ed25519 cryptographic signature verification (`pynacl`).
- **Async Self-Invocation Worker**: Solves Discord's 3-second timeout by returning an immediate deferred ACK (`type: 5` *"Thinking..."*) in under 150ms, then invokes itself asynchronously to run the agent and update the original response via Discord webhook.
- **Cloud SQLite Persistence**: Automatically synchronizes the local `wfm.db` SQLite database with Amazon S3 cache on every write.
- **Scheduled Monitoring**: Amazon EventBridge daily cron triggers batch evaluations and dispatches Discord alerts for price threshold hits and Resurgence countdowns ($\le 7$ days).

---

## Quickstart

### 1. Installation
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
OPENROUTER_API_KEY=your_openrouter_api_key_here  # Optional fallback

# Discord Bot Credentials
DISCORD_APPLICATION_ID=your_application_id_here
DISCORD_PUBLIC_KEY=your_public_key_hex_here
DISCORD_BOT_TOKEN=your_bot_token_here
DISCORD_WEBHOOK_URL=your_discord_webhook_url_here  # For batch channel alerts
```

### 3. Usage

#### Interactive CLI Shell:
```bash
python main.py -i
```

#### One-Shot CLI Queries:
```bash
# Baseline SELL/HOLD cards
python main.py -q "rhino prime"
python main.py -q "soma prime"

# Conversational Agent Queries
python main.py -q "Is 45p fair for Rhino Prime Blueprint?"
python main.py -q "Sell Saryn Prime as set or parts?"
python main.py -q "Is Volt Prime vaulted?"
python main.py -q "Compare Rhino Prime vs Saryn Prime"
```

#### Registering Discord Slash Commands:
```bash
python -m discord_bot.register_commands
```

#### Deploying to AWS Lambda:
```bash
bash scripts/deploy_aws.sh
```

---

## Running Tests
```bash
pytest
```
All **72 automated tests** cover slug resolution, cache TTLs, domain market tools, conversational routing, Discord Ed25519 signature validation, and Lambda execution paths.

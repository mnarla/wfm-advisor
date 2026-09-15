"""
lambda_handler.py — AWS Lambda serverless execution entrypoint for WFM Sell-Timing Advisor.

Supports:
1. On-demand HTTP queries via Lambda Function URL (e.g. GET /?query=rhino+prime).
2. Scheduled batch evaluations via Amazon EventBridge (e.g. daily cron at 02:00 UTC).
3. S3 round-trip SQLite cache persistence (/tmp/wfm.db <-> s3://wfm-advisor-cache/wfm.db).
4. Secure credential resolution via AWS SSM Parameter Store.
5. Best-effort Discord webhook alert notifications for SELL recommendations.
"""

import os
import json
import shutil
import logging
import sqlite3
from typing import Dict, Any, List, Optional

import boto3
from botocore.exceptions import ClientError, BotoCoreError
import requests

from ingest.cache_manager import get_recommendation, format_recommendation_card
from nodes.graph import create_advisor_graph
from discord_bot.signature import verify_discord_signature
from discord_bot.embeds import (
    build_advice_embed,
    build_fair_price_embed,
    build_parts_embed,
    build_vault_embed,
    build_agent_embed,
)
from nodes.tools import check_fair_price, compare_set_vs_parts, get_vault_status

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Configuration & Environment Defaults
AWS_REGION = os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "us-west-2"))
S3_CACHE_BUCKET = os.getenv("S3_CACHE_BUCKET", "wfm-advisor-cache")
S3_CACHE_KEY = os.getenv("S3_CACHE_KEY", "wfm.db")
DB_PATH = os.getenv("DB_PATH", "/tmp/wfm.db" if "AWS_LAMBDA_FUNCTION_NAME" in os.environ else "db/wfm.db")
SSM_GEMINI_KEY_PARAM = os.getenv("SSM_GEMINI_KEY_PARAM", "/wfmadvisor/gemini_api_key")
SSM_DISCORD_PARAM = os.getenv("SSM_DISCORD_PARAM", "/wfmadvisor/discord_webhook_url")
SSM_DISCORD_PUBLIC_KEY_PARAM = os.getenv("SSM_DISCORD_PUBLIC_KEY_PARAM", "/wfmadvisor/discord_public_key")
WATCHLIST_PATH = os.getenv("WATCHLIST_PATH", "config/watchlist.json")

# In-memory SSM parameter cache across warm Lambda invocations
_SSM_CACHE: Dict[str, str] = {}


def get_ssm_parameter(param_name: str, ssm_client: Optional[Any] = None) -> Optional[str]:
    """
    Fetches a parameter from AWS SSM Parameter Store with decryption.
    Results are cached in memory for warm Lambda containers.
    """
    if param_name in _SSM_CACHE:
        return _SSM_CACHE[param_name]

    if not ssm_client:
        try:
            ssm_client = boto3.client("ssm", region_name=AWS_REGION)
        except BotoCoreError as e:
            logger.warning(f"Could not create SSM client ({e}); skipping parameter fetch.")
            return None

    try:
        response = ssm_client.get_parameter(Name=param_name, WithDecryption=True)
        val = response["Parameter"]["Value"]
        _SSM_CACHE[param_name] = val
        return val
    except (ClientError, BotoCoreError) as e:
        logger.warning(f"Failed to fetch SSM parameter '{param_name}': {e}")
        return None
    except Exception as e:
        logger.warning(f"Unexpected error fetching SSM parameter '{param_name}': {e}")
        return None


def init_credentials() -> None:
    """
    Initializes external API credentials (Gemini, Discord) from SSM Parameter Store
    if they are not already set in the execution environment.
    """
    if not os.getenv("GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY") == "your_gemini_api_key_here":
        gemini_key = get_ssm_parameter(SSM_GEMINI_KEY_PARAM)
        if gemini_key:
            os.environ["GEMINI_API_KEY"] = gemini_key
            logger.info("Loaded GEMINI_API_KEY from SSM Parameter Store.")

    if not os.getenv("DISCORD_WEBHOOK_URL"):
        discord_url = get_ssm_parameter(SSM_DISCORD_PARAM)
        if discord_url:
            os.environ["DISCORD_WEBHOOK_URL"] = discord_url
            logger.info("Loaded DISCORD_WEBHOOK_URL from SSM Parameter Store.")

    if not os.getenv("DISCORD_PUBLIC_KEY"):
        discord_pub = get_ssm_parameter(SSM_DISCORD_PUBLIC_KEY_PARAM)
        if discord_pub:
            os.environ["DISCORD_PUBLIC_KEY"] = discord_pub
            logger.info("Loaded DISCORD_PUBLIC_KEY from SSM Parameter Store.")


def ensure_db_available(s3_client: Optional[Any] = None) -> None:
    """
    Ensures a valid SQLite database exists at DB_PATH on Lambda cold start:
    1. If DB_PATH already exists (e.g. warm container or local), do nothing.
    2. Attempt to download the latest wfm.db from the S3 cache bucket.
    3. If S3 download fails (e.g. first run / bucket empty), fall back to bundled seed DB.
    """
    if os.path.exists(DB_PATH):
        return

    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)

    if not s3_client:
        try:
            s3_client = boto3.client("s3", region_name=AWS_REGION)
        except BotoCoreError as e:
            logger.warning(f"Could not create S3 client ({e}); skipping S3 download.")

    downloaded = False
    try:
        if s3_client:
            logger.info(f"Attempting to pull latest SQLite cache from s3://{S3_CACHE_BUCKET}/{S3_CACHE_KEY}...")
            s3_client.download_file(S3_CACHE_BUCKET, S3_CACHE_KEY, DB_PATH)
            downloaded = True
            logger.info("Successfully restored wfm.db from S3 cache.")
    except (ClientError, BotoCoreError) as e:
        logger.warning(f"S3 cache download failed: {e}. Falling back to bundled seed.")
    except Exception as e:
        logger.warning(f"Unexpected error downloading from S3: {e}. Falling back to bundled seed.")

    if not downloaded:
        seed_path = os.path.join(os.path.dirname(__file__), "db", "wfm.db")
        if os.path.exists(seed_path):
            shutil.copy2(seed_path, DB_PATH)
            logger.info(f"Initialized {DB_PATH} from bundled seed database.")
        else:
            logger.error(f"Neither S3 cache nor bundled seed database found at {seed_path}!")


def sync_db_to_s3(s3_client: Optional[Any] = None) -> bool:
    """
    Uploads the updated SQLite database from DB_PATH back to S3.
    """
    if not os.path.exists(DB_PATH):
        logger.warning(f"Cannot sync to S3: {DB_PATH} does not exist.")
        return False

    if not s3_client:
        try:
            s3_client = boto3.client("s3", region_name=AWS_REGION)
        except BotoCoreError as e:
            logger.warning(f"Could not create S3 client ({e}); skipping S3 upload.")
            return False

    try:
        logger.info(f"Uploading updated SQLite cache to s3://{S3_CACHE_BUCKET}/{S3_CACHE_KEY}...")
        s3_client.upload_file(DB_PATH, S3_CACHE_BUCKET, S3_CACHE_KEY)
        logger.info("Successfully synced wfm.db to S3.")
        return True
    except Exception as e:
        logger.error(f"Failed to upload wfm.db to S3: {e}", exc_info=True)
        return False


def send_discord_alert(
    sell_items: List[Dict[str, Any]],
    total_scanned: int,
    resurgence_alerts: Optional[List[Dict[str, Any]]] = None,
    threshold_alerts: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    """
    Sends a rich Discord embed summary to DISCORD_WEBHOOK_URL.
    Best-effort: errors are logged, never failing the Lambda invocation.
    Includes sections for SELL recommendations, imminent resurgence expirations,
    and price-threshold alerts.
    """
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook_url or webhook_url.startswith("your_"):
        logger.info("No valid DISCORD_WEBHOOK_URL configured; skipping Discord notification.")
        return False

    resurgence_alerts = resurgence_alerts or []
    threshold_alerts = threshold_alerts or []
    now_utc = os.popen("date -u +'%Y-%m-%d %H:%M UTC'").read().strip() if hasattr(os, "popen") else "Daily Scan"

    has_alerts = bool(sell_items or resurgence_alerts or threshold_alerts)

    if not has_alerts:
        embed = {
            "title": "🛡️ Warframe Market Daily Scan: All Items Held",
            "description": f"Completed automated market scan across **{total_scanned} watchlisted items**. Zero sell triggers or price thresholds met; continue holding all inventory.",
            "color": 0x3498DB,  # Blue
            "footer": {"text": f"WFM Sell-Timing Advisor • {now_utc}"},
        }
    else:
        fields = []

        # 1. SELL Signals
        for item in sell_items[:10]:
            price_str = f"{item.get('current_price', 'N/A')}p"
            trend = item.get("pct_change_90d")
            trend_str = f"+{trend}%" if trend and trend > 0 else f"{trend}%" if trend else "N/A"
            driver = item.get("primary_driver", "momentum").replace("_", " ").title()
            conf = item.get("confidence", "high").title()

            fields.append({
                "name": f"🚨 {item['item_name']} — SELL ({conf} Conf)",
                "value": (
                    f"**Median Price:** `{price_str}` | **90d Trend:** `{trend_str}`\n"
                    f"**Primary Driver:** `{driver}`\n"
                    f"> {item.get('reasoning', 'No reasoning provided.')}"
                ),
                "inline": False,
            })

        # 2. Resurgence Countdown Alerts (<= 7 days remaining)
        for resurg in resurgence_alerts[:5]:
            fields.append({
                "name": f"⏳ {resurg['item_name']} — Resurgence Ending Soon!",
                "value": (
                    f"**Remaining:** `{resurg.get('days_remaining')} day(s)`\n"
                    f"> Rotation closing soon. Unvaulting supply influx will end and prices may begin recovering."
                ),
                "inline": False,
            })

        # 3. Price Threshold Alerts
        for thresh in threshold_alerts[:5]:
            t_type = thresh.get("type", "sell")
            icon = "🎯" if t_type == "sell" else "🛒"
            fields.append({
                "name": f"{icon} {thresh['item_name']} — Target {t_type.upper()} Price Reached!",
                "value": (
                    f"**Current:** `{thresh.get('current_price')}p` | **Target:** `{thresh.get('target_price')}p`\n"
                    f"> Current market price meets your configured watchlist threshold."
                ),
                "inline": False,
            })

        if sell_items and not resurgence_alerts and not threshold_alerts:
            title = f"🚨 Warframe Market Alert: {len(sell_items)} SELL Signal(s) Detected!"
            description = f"The automated daily scan evaluated **{total_scanned} watchlisted items** and identified **{len(sell_items)}** prime selling opportunity(s):"
        else:
            title_parts = []
            if sell_items:
                title_parts.append(f"{len(sell_items)} SELL")
            if resurgence_alerts:
                title_parts.append(f"{len(resurgence_alerts)} Resurgence")
            if threshold_alerts:
                title_parts.append(f"{len(threshold_alerts)} Threshold")

            title = f"🚨 Warframe Market Alert: {', '.join(title_parts)} Alert(s) Detected!"
            description = f"Evaluated **{total_scanned} watchlisted items** and identified key opportunities:"

        embed = {
            "title": title,
            "description": description,
            "color": 0xE74C3C if sell_items else 0xF39C12,  # Red if sell, orange otherwise
            "fields": fields[:25],
            "footer": {"text": f"WFM Sell-Timing Advisor • {now_utc}"},
        }

    payload = {
        "username": "WFM Sell-Timing Advisor",
        "avatar_url": "https://warframe.market/static/assets/user/default-avatar.png",
        "embeds": [embed],
    }

    try:
        res = requests.post(webhook_url, json=payload, timeout=10)
        res.raise_for_status()
        logger.info(f"Successfully posted Discord alert.")
        return True
    except Exception as e:
        logger.error(f"Failed to post alert to Discord webhook: {e}")
        return False


def run_batch_evaluation() -> Dict[str, Any]:
    """
    Executes a batch evaluation over the items in config/watchlist.json.
    Supports both string item names and structured threshold objects.
    Returns summary statistics and lists of SELL recommendations, resurgence warnings,
    and price-threshold triggers.
    """
    watchlist = []
    if os.path.exists(WATCHLIST_PATH):
        try:
            with open(WATCHLIST_PATH, "r") as f:
                watchlist = json.load(f)
        except Exception as e:
            logger.error(f"Failed to read {WATCHLIST_PATH}: {e}")

    if not watchlist:
        # Fallback to querying distinct frames from items table
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT frame_name FROM items WHERE frame_name IS NOT NULL LIMIT 15")
        watchlist = [r[0] for r in cur.fetchall()]
        conn.close()

    logger.info(f"Starting batch evaluation for {len(watchlist)} watchlist items...")

    evaluated_items = []
    sell_items = []
    resurgence_alerts = []
    threshold_alerts = []

    for idx, entry in enumerate(watchlist, 1):
        if isinstance(entry, dict):
            frame = entry.get("name", "")
            target_sell = entry.get("target_sell_price")
            target_buy = entry.get("target_buy_price")
        else:
            frame = str(entry)
            target_sell = None
            target_buy = None

        if not frame:
            continue

        try:
            logger.info(f"[{idx}/{len(watchlist)}] Evaluating {frame}...")
            res = get_recommendation(frame, db_path=DB_PATH)
            if res.get("status") != "resolved":
                continue

            results_list = res.get("results") or [res]
            for item_res in results_list:
                rec = item_res.get("recommendation", "HOLD")
                evaluated_items.append(item_res)
                trend = item_res.get("trend_signal", {})
                vault = item_res.get("vault_signal", {})
                curr_p = trend.get("current_price")

                if rec == "SELL":
                    sell_items.append({
                        "item_name": item_res.get("item_name") or frame,
                        "component_type": item_res.get("component_type", "Set"),
                        "current_price": curr_p,
                        "pct_change_90d": trend.get("pct_change_90d"),
                        "primary_driver": item_res.get("primary_driver", "trend"),
                        "confidence": trend.get("confidence", "high"),
                        "reasoning": item_res.get("reasoning", ""),
                    })

                # Check Resurgence countdown alert (<= 7 days remaining)
                if vault.get("is_resurgence_active"):
                    days_until = vault.get("days_until_vault")
                    if days_until is not None and 0 <= days_until <= 7:
                        resurgence_alerts.append({
                            "item_name": item_res.get("item_name") or frame,
                            "days_remaining": days_until,
                        })

                # Check price threshold alerts
                if curr_p is not None:
                    if target_sell is not None and curr_p >= target_sell:
                        threshold_alerts.append({
                            "item_name": item_res.get("item_name") or frame,
                            "type": "sell",
                            "current_price": curr_p,
                            "target_price": target_sell,
                        })
                    elif target_buy is not None and curr_p <= target_buy:
                        threshold_alerts.append({
                            "item_name": item_res.get("item_name") or frame,
                            "type": "buy",
                            "current_price": curr_p,
                            "target_price": target_buy,
                        })

        except Exception as e:
            logger.error(f"Error evaluating '{frame}' in batch: {e}", exc_info=True)

    # Post Discord alerts (best-effort)
    send_discord_alert(
        sell_items,
        len(evaluated_items),
        resurgence_alerts=resurgence_alerts,
        threshold_alerts=threshold_alerts,
    )

    # Persist updated SQLite cache to S3
    sync_db_to_s3()

    return {
        "status": "success",
        "mode": "batch",
        "total_scanned": len(evaluated_items),
        "sell_count": len(sell_items),
        "sell_items": sell_items,
        "resurgence_alerts": resurgence_alerts,
        "threshold_alerts": threshold_alerts,
    }


def handle_query_request(user_query: str) -> Dict[str, Any]:
    """
    Handles an on-demand single item recommendation query.
    Uploads to S3 only if the SQLite cache file was modified during execution.
    """
    initial_mtime = os.path.getmtime(DB_PATH) if os.path.exists(DB_PATH) else 0

    result = get_recommendation(user_query, db_path=DB_PATH)

    new_mtime = os.path.getmtime(DB_PATH) if os.path.exists(DB_PATH) else 0
    if new_mtime > initial_mtime:
        logger.info("Database cache was updated during query; syncing to S3...")
        sync_db_to_s3()
    else:
        logger.info("Database cache was fresh; skipped S3 upload.")

    return result



def handle_discord_worker(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Executes Discord slash command logic asynchronously and patches original deferred message.
    """
    init_credentials()
    ensure_db_available()

    initial_mtime = os.path.getmtime(DB_PATH) if os.path.exists(DB_PATH) else 0

    command = event.get("command", "")
    options = event.get("options") or {}
    app_id = event.get("application_id")
    token = event.get("token")

    logger.info(f"Processing discord_worker command '/{command}' with options: {options}")

    embed = None
    try:
        if command == "ask":
            query = options.get("query") or options.get("item") or event.get("query") or event.get("item") or ""
            rec = get_recommendation(query, db_path=DB_PATH)
            if rec.get("agent_mode"):
                text = rec.get("response") or rec.get("formatted_card", "")
                embed = build_agent_embed(query, text)
            elif rec.get("status") == "resolved":
                embed = build_advice_embed(rec)
            else:
                card = rec.get("formatted_card") or f"Unable to find market information for '{query}'."
                embed = build_agent_embed(query, card)

        elif command == "card":
            item = options.get("item") or options.get("query") or event.get("item") or event.get("query") or ""
            rec = get_recommendation(item, db_path=DB_PATH)
            if rec.get("status") == "resolved":
                embed = build_advice_embed(rec)
            else:
                embed = build_agent_embed(item, f"Could not find item '{item}'. Please check spelling.")

        elif command == "fair":
            item = options.get("item") or event.get("item") or ""
            price_val = options.get("price") if "price" in options else event.get("price", 0.0)
            try:
                price_float = float(price_val)
            except (ValueError, TypeError):
                price_float = 0.0

            analysis_text = check_fair_price.invoke({"item_name": item, "offered_price": price_float})
            embed = build_fair_price_embed(item, price_float, analysis_text)

        elif command == "parts":
            item = options.get("item") or options.get("query") or event.get("item") or ""
            breakdown = compare_set_vs_parts.invoke({"item_name": item})
            embed = build_parts_embed(item, breakdown)

        elif command == "vault":
            item = options.get("item") or options.get("query") or event.get("item") or ""
            vault_text = get_vault_status.invoke({"item_name": item})
            embed = build_vault_embed(item, vault_text)

        else:
            embed = build_agent_embed(command, f"Unknown slash command: /{command}")

    except Exception as e:
        logger.error(f"Error executing command '{command}': {e}", exc_info=True)
        embed = build_agent_embed(command, f"Error processing command: {str(e)}")

    new_mtime = os.path.getmtime(DB_PATH) if os.path.exists(DB_PATH) else 0
    if new_mtime > initial_mtime:
        logger.info("Database cache was updated during Discord worker execution; syncing to S3...")
        sync_db_to_s3()

    if app_id and token:
        webhook_url = f"https://discord.com/api/v10/webhooks/{app_id}/{token}/messages/@original"
        payload = {"embeds": [embed]}
        try:
            res = requests.patch(
                webhook_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            res.raise_for_status()
            logger.info(f"Successfully posted embed to Discord webhook: {webhook_url}")
        except Exception as e:
            logger.error(f"Failed to patch Discord original message: {e}", exc_info=True)

    return {
        "status": "success",
        "command": command,
        "embed": embed,
    }


def lambda_handler(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    """
    Main Lambda entrypoint.
    Dispatches between EventBridge cron / batch triggers, HTTP API queries,
    and Discord Slash Command interactions.
    """
    # 1. Check if event is an internal async discord worker execution
    if event.get("discord_worker"):
        return handle_discord_worker(event)

    try:
        logger.info(f"Received Lambda event: {json.dumps(event)}")
    except Exception:
        logger.info(f"Received Lambda event: {event}")

    # 2. Initialize credentials from SSM Parameter Store if needed
    init_credentials()

    # 3. Check for Discord HTTP headers: x-signature-ed25519 and x-signature-timestamp (case-insensitive)
    headers = {str(k).lower(): str(v) for k, v in (event.get("headers") or {}).items()}
    sig = headers.get("x-signature-ed25519")
    timestamp = headers.get("x-signature-timestamp")

    if sig and timestamp:
        public_key = os.getenv("DISCORD_PUBLIC_KEY") or get_ssm_parameter(SSM_DISCORD_PUBLIC_KEY_PARAM) or ""
        body_raw = event.get("body", "")
        if event.get("isBase64Encoded"):
            import base64
            try:
                body_raw = base64.b64decode(body_raw).decode("utf-8")
            except Exception as e:
                logger.warning(f"Failed to decode base64 Discord request body: {e}")

        if not verify_discord_signature(sig, timestamp, body_raw, public_key):
            logger.warning("Invalid Discord signature; returning 401.")
            return {
                "statusCode": 401,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"error": "invalid request signature"}),
            }

        try:
            body_json = json.loads(body_raw) if isinstance(body_raw, str) else (body_raw or {})
        except Exception as e:
            logger.error(f"Malformed JSON in Discord interaction request: {e}")
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"error": "malformed JSON body"}),
            }

        interaction_type = body_json.get("type")

        # Type 1: PING -> PONG
        if interaction_type == 1:
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"type": 1}),
            }

        # Type 2: APPLICATION_COMMAND
        if interaction_type == 2:
            app_id = body_json.get("application_id")
            token = body_json.get("token")
            data = body_json.get("data", {})
            command_name = data.get("name")
            options_list = data.get("options", [])
            options_dict = {opt["name"]: opt.get("value") for opt in options_list if "name" in opt}

            worker_payload = {
                "discord_worker": True,
                "command": command_name,
                "options": options_dict,
                "application_id": app_id,
                "token": token,
            }

            function_name = getattr(context, "function_name", None) if context else None
            if not function_name and os.getenv("AWS_LAMBDA_FUNCTION_NAME"):
                function_name = os.getenv("AWS_LAMBDA_FUNCTION_NAME")

            if function_name:
                try:
                    lambda_client = boto3.client("lambda", region_name=AWS_REGION)
                    lambda_client.invoke(
                        FunctionName=function_name,
                        InvocationType="Event",
                        Payload=json.dumps(worker_payload),
                    )
                    logger.info(f"Dispatched async worker invocation to Lambda function '{function_name}'.")
                except Exception as e:
                    logger.error(f"Failed to invoke Lambda async worker ({e}), falling back to background thread.")
                    import threading
                    threading.Thread(target=handle_discord_worker, args=(worker_payload,)).start()
            else:
                import threading
                threading.Thread(target=handle_discord_worker, args=(worker_payload,)).start()

            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"type": 5}),
            }

    # 4. Ensure SQLite cache exists (pulling from S3 or seed DB)
    ensure_db_available()

    # 5. Detect invocation mode
    # EventBridge scheduled cron sends {"detail-type": "Scheduled Event"} or custom {"mode": "batch"}
    is_scheduled = (
        event.get("mode") == "batch"
        or event.get("detail-type") == "Scheduled Event"
        or event.get("source") == "aws.events"
    )

    if is_scheduled:
        batch_summary = run_batch_evaluation()
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(batch_summary),
        }

    # 4. Handle HTTP Query (Lambda Function URL or API Gateway)
    query = None

    # Check query string parameters (GET /?query=rhino+prime)
    query_params = event.get("queryStringParameters") or {}
    if "query" in query_params:
        query = query_params["query"]
    elif "item" in query_params:
        query = query_params["item"]

    # Check request body (POST {"query": "rhino prime"})
    if not query and event.get("body"):
        try:
            body = json.loads(event["body"]) if isinstance(event["body"], str) else event["body"]
            query = body.get("query") or body.get("item")
        except Exception:
            pass

    # Check direct payload invoke (e.g. {"query": "rhino prime"})
    if not query:
        query = event.get("query") or event.get("item")

    if not query:
        return {
            "statusCode": 400,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*",
            },
            "body": json.dumps({
                "error": "Missing 'query' parameter.",
                "usage": "Provide 'query' as a query string (GET /?query=rhino+prime) or in JSON body (POST {\"query\": \"rhino prime\"}).",
            }),
        }

    try:
        rec_data = handle_query_request(query)
        return {
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            },
            "body": json.dumps(rec_data),
        }
    except Exception as e:
        logger.error(f"Error processing query '{query}': {e}", exc_info=True)
        return {
            "statusCode": 500,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*",
            },
            "body": json.dumps({"error": f"Internal execution error: {str(e)}"}),
        }

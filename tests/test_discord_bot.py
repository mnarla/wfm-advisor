"""
tests/test_discord_bot.py — Comprehensive unit tests for Discord Slash Commands integration:
- Signature verification (valid, invalid, malformed hex, tampered body)
- Embed formatters (advice embed colors/fields, fair price, parts, vault, agent)
- Command registration script
- lambda_handler routing (ping->pong, command->type 5, invalid sig->401)
- handle_discord_worker async execution and webhook PATCH
"""

import os
import json
import pytest
from unittest.mock import MagicMock, patch
from nacl.signing import SigningKey

from discord_bot.signature import verify_discord_signature
from discord_bot.embeds import (
    build_advice_embed,
    build_fair_price_embed,
    build_parts_embed,
    build_vault_embed,
    build_agent_embed,
    COLOR_SELL,
    COLOR_HOLD,
    COLOR_BUY,
    COLOR_BLUE,
    COLOR_PURPLE,
    COLOR_YELLOW,
)
from discord_bot.register_commands import register_commands, COMMANDS
import lambda_handler


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def ed25519_keypair():
    signing_key = SigningKey.generate()
    verify_key = signing_key.verify_key
    pub_hex = verify_key.encode().hex()
    return signing_key, pub_hex


# ---------------------------------------------------------------------------
# 1. Signature Verification Tests
# ---------------------------------------------------------------------------

def test_verify_discord_signature_valid(ed25519_keypair):
    signing_key, pub_hex = ed25519_keypair
    timestamp = "1726330000"
    body = '{"type": 1}'
    message = f"{timestamp}{body}".encode("utf-8")
    sig_hex = signing_key.sign(message).signature.hex()

    is_valid = verify_discord_signature(sig_hex, timestamp, body, pub_hex)
    assert is_valid is True


def test_verify_discord_signature_invalid_sig(ed25519_keypair):
    _, pub_hex = ed25519_keypair
    timestamp = "1726330000"
    body = '{"type": 1}'
    fake_sig = "ab" * 64

    is_valid = verify_discord_signature(fake_sig, timestamp, body, pub_hex)
    assert is_valid is False


def test_verify_discord_signature_tampered_body(ed25519_keypair):
    signing_key, pub_hex = ed25519_keypair
    timestamp = "1726330000"
    body = '{"type": 1}'
    message = f"{timestamp}{body}".encode("utf-8")
    sig_hex = signing_key.sign(message).signature.hex()

    # Tamper with body
    is_valid = verify_discord_signature(sig_hex, timestamp, '{"type": 2}', pub_hex)
    assert is_valid is False


def test_verify_discord_signature_bad_hex_and_none(ed25519_keypair):
    _, pub_hex = ed25519_keypair

    # Non-hex characters
    assert verify_discord_signature("invalid_hex_string", "123", "body", pub_hex) is False
    assert verify_discord_signature("12345", "123", "body", "invalid_pub_hex") is False

    # Empty and None values
    assert verify_discord_signature("", "123", "body", pub_hex) is False
    assert verify_discord_signature("1234", "", "body", pub_hex) is False
    assert verify_discord_signature("1234", "123", "", pub_hex) is False
    assert verify_discord_signature("1234", "123", "body", "") is False


# ---------------------------------------------------------------------------
# 2. Discord Embed Builders Tests
# ---------------------------------------------------------------------------

def test_build_advice_embed_colors_and_fields():
    # SELL
    sell_rec = {
        "item_name": "Rhino Prime Set",
        "slug": "rhino_prime_set",
        "recommendation": "SELL",
        "reasoning": "Peak price reached before Resurgence.",
        "trend_signal": {"current_price": 125, "pct_change_90d": 35.0},
        "vault_signal": {"signal": "vaulted", "reasoning": "Vaulted 400 days"},
        "patch_signal": {"expected_impact": "None"},
    }
    embed_sell = build_advice_embed(sell_rec)
    assert embed_sell["color"] == COLOR_SELL
    assert "Rhino Prime Set" in embed_sell["title"]
    field_names = [f["name"] for f in embed_sell["fields"]]
    assert "Price" in field_names
    assert "90d Trend" in field_names
    assert "Vault" in field_names
    assert "Patch" in field_names
    assert "Action + Reasoning" in field_names
    action_field = next(f for f in embed_sell["fields"] if f["name"] == "Action + Reasoning")
    assert "**SELL**" in action_field["value"]

    # HOLD
    hold_rec = dict(sell_rec, recommendation="HOLD")
    embed_hold = build_advice_embed(hold_rec)
    assert embed_hold["color"] == COLOR_HOLD

    # BUY
    buy_rec = dict(sell_rec, recommendation="BUY")
    embed_buy = build_advice_embed(buy_rec)
    assert embed_buy["color"] == COLOR_BUY


def test_build_advice_embed_unwraps_results_list():
    rec_wrapper = {
        "status": "resolved",
        "results": [
            {
                "item_name": "Saryn Prime Set",
                "recommendation": "HOLD",
                "reasoning": "Holding for next rotation.",
                "signal_summary": {
                    "price": "180p",
                    "trend": "+10%",
                    "vault": "Vaulted",
                    "patch": "None",
                },
            }
        ],
    }
    embed = build_advice_embed(rec_wrapper)
    assert embed["color"] == COLOR_HOLD
    assert "Saryn Prime Set" in embed["title"]


def test_build_fair_price_embed():
    # BARGAIN (Green)
    embed_bargain = build_fair_price_embed("Rhino Prime", 50.0, "Verdict: ✅ BARGAIN — 40% below market median")
    assert embed_bargain["color"] == COLOR_BUY
    assert "Rhino Prime" in embed_bargain["title"]
    assert "50p" in embed_bargain["fields"][0]["value"]

    # OVERPRICED (Red)
    embed_overpriced = build_fair_price_embed("Rhino Prime", 150.0, "Verdict: ⚠️ OVERPRICED by 50% vs market")
    assert embed_overpriced["color"] == COLOR_SELL

    # FAIR (Blue)
    embed_fair = build_fair_price_embed("Rhino Prime", 100.0, "Verdict: ✅ FAIR — within ±20%")
    assert embed_fair["color"] == COLOR_BLUE


def test_build_parts_embed():
    embed = build_parts_embed("Rhino Prime", "Set Price: 120p\nSum of Parts: 145p\nAdvice: Sell as parts!")
    assert embed["color"] == COLOR_PURPLE
    assert "Rhino Prime" in embed["title"]
    assert "Sum of Parts: 145p" in embed["description"]


def test_build_vault_embed():
    embed = build_vault_embed("Rhino Prime", "Signal: recently_vaulted\nDays Until Vault: 0\nSummary: Vaulted.")
    assert embed["color"] == COLOR_YELLOW
    assert "Rhino Prime" in embed["title"]
    assert "recently_vaulted" in embed["description"]


def test_build_agent_embed():
    embed = build_agent_embed("Is Rhino Prime worth buying?", "Based on 90-day trends, Rhino Prime is steady...")
    assert "Is Rhino Prime worth buying?" in embed["title"]
    assert "steady" in embed["description"]


# ---------------------------------------------------------------------------
# 3. Slash Command Registration Tests
# ---------------------------------------------------------------------------

def test_register_commands_posts_all_commands():
    assert len(COMMANDS) == 5
    cmd_names = {c["name"] for c in COMMANDS}
    assert cmd_names == {"ask", "card", "fair", "parts", "vault"}

    with patch("requests.post") as mock_post:
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"id": "cmd_123"}
        mock_post.return_value = mock_response

        registered = register_commands("test_app_id", "test_bot_token")
        assert len(registered) == 5
        assert mock_post.call_count == 5
        first_call_headers = mock_post.call_args_list[0][1]["headers"]
        assert first_call_headers["Authorization"] == "Bot test_bot_token"


# ---------------------------------------------------------------------------
# 4. lambda_handler Discord Routing Tests
# ---------------------------------------------------------------------------

def test_lambda_handler_discord_ping_returns_pong(ed25519_keypair):
    signing_key, pub_hex = ed25519_keypair
    timestamp = "1726330000"
    body = json.dumps({"type": 1})
    sig_hex = signing_key.sign(f"{timestamp}{body}".encode()).signature.hex()

    event = {
        "headers": {
            "x-signature-ed25519": sig_hex,
            "x-signature-timestamp": timestamp,
        },
        "body": body,
    }

    with patch.dict(os.environ, {"DISCORD_PUBLIC_KEY": pub_hex}), \
         patch.object(lambda_handler, "init_credentials"), \
         patch.object(lambda_handler, "ensure_db_available"):
        resp = lambda_handler.lambda_handler(event, None)
        assert resp["statusCode"] == 200
        data = json.loads(resp["body"])
        assert data["type"] == 1


def test_lambda_handler_discord_invalid_signature_returns_401(ed25519_keypair):
    _, pub_hex = ed25519_keypair
    event = {
        "headers": {
            "x-signature-ed25519": "bad_sig" * 16,
            "x-signature-timestamp": "1726330000",
        },
        "body": '{"type": 1}',
    }

    with patch.dict(os.environ, {"DISCORD_PUBLIC_KEY": pub_hex}), \
         patch.object(lambda_handler, "init_credentials"):
        resp = lambda_handler.lambda_handler(event, None)
        assert resp["statusCode"] == 401
        data = json.loads(resp["body"])
        assert "invalid request signature" in data["error"]


def test_lambda_handler_discord_command_returns_type_5_and_invokes_lambda(ed25519_keypair):
    signing_key, pub_hex = ed25519_keypair
    timestamp = "1726330000"
    interaction_body = json.dumps({
        "type": 2,
        "application_id": "app_999",
        "token": "token_abc",
        "data": {
            "name": "card",
            "options": [{"name": "item", "value": "Rhino Prime"}],
        },
    })
    sig_hex = signing_key.sign(f"{timestamp}{interaction_body}".encode()).signature.hex()

    event = {
        "headers": {
            "X-Signature-Ed25519": sig_hex,  # Test uppercase header name
            "X-Signature-Timestamp": timestamp,
        },
        "body": interaction_body,
    }

    mock_context = MagicMock()
    mock_context.function_name = "wfm-advisor-prod"

    with patch.dict(os.environ, {"DISCORD_PUBLIC_KEY": pub_hex}), \
         patch.object(lambda_handler, "init_credentials"), \
         patch.object(lambda_handler, "ensure_db_available"), \
         patch("boto3.client") as mock_boto:
        mock_lambda_client = MagicMock()
        mock_boto.return_value = mock_lambda_client

        resp = lambda_handler.lambda_handler(event, mock_context)
        assert resp["statusCode"] == 200
        data = json.loads(resp["body"])
        assert data["type"] == 5

        # Verify Lambda async invocation
        mock_lambda_client.invoke.assert_called_once()
        call_kwargs = mock_lambda_client.invoke.call_args[1]
        assert call_kwargs["FunctionName"] == "wfm-advisor-prod"
        assert call_kwargs["InvocationType"] == "Event"
        payload = json.loads(call_kwargs["Payload"])
        assert payload["discord_worker"] is True
        assert payload["command"] == "card"
        assert payload["options"]["item"] == "Rhino Prime"
        assert payload["application_id"] == "app_999"
        assert payload["token"] == "token_abc"


# ---------------------------------------------------------------------------
# 5. handle_discord_worker Tests
# ---------------------------------------------------------------------------

def test_handle_discord_worker_dispatches_and_patches_webhook():
    worker_event = {
        "discord_worker": True,
        "command": "card",
        "options": {"item": "Rhino Prime"},
        "application_id": "app_123",
        "token": "token_456",
    }

    mock_rec = {
        "status": "resolved",
        "item_name": "Rhino Prime Set",
        "slug": "rhino_prime_set",
        "recommendation": "SELL",
        "reasoning": "Price at 90d peak.",
        "trend_signal": {"current_price": 120, "pct_change_90d": 25.0},
        "vault_signal": {"signal": "vaulted", "reasoning": "Vaulted 300 days"},
        "patch_signal": {"expected_impact": "None"},
    }

    with patch.object(lambda_handler, "init_credentials"), \
         patch.object(lambda_handler, "ensure_db_available"), \
         patch.object(lambda_handler, "get_recommendation", return_value=mock_rec), \
         patch("requests.patch") as mock_patch:
        mock_patch.return_value.status_code = 200

        result = lambda_handler.handle_discord_worker(worker_event)
        assert result["status"] == "success"
        assert result["command"] == "card"
        assert result["embed"]["color"] == COLOR_SELL

        # Verify webhook PATCH call
        mock_patch.assert_called_once()
        url = mock_patch.call_args[0][0]
        assert url == "https://discord.com/api/v10/webhooks/app_123/token_456/messages/@original"
        payload = mock_patch.call_args[1]["json"]
        assert "embeds" in payload
        assert len(payload["embeds"]) == 1


def test_handle_discord_worker_fair_price_command():
    worker_event = {
        "discord_worker": True,
        "command": "fair",
        "options": {"item": "Rhino Prime", "price": 50.0},
        "application_id": "app_123",
        "token": "token_456",
    }

    with patch.object(lambda_handler, "init_credentials"), \
         patch.object(lambda_handler, "ensure_db_available"), \
         patch("lambda_handler.check_fair_price") as mock_tool, \
         patch("requests.patch") as mock_patch:
        mock_tool.invoke.return_value = "Verdict: ✅ BARGAIN — 40% below median"
        mock_patch.return_value.status_code = 200

        result = lambda_handler.handle_discord_worker(worker_event)
        assert result["status"] == "success"
        assert result["embed"]["color"] == COLOR_BUY


def test_handle_discord_worker_parts_command():
    worker_event = {
        "discord_worker": True,
        "command": "parts",
        "options": {"item": "Rhino Prime"},
        "application_id": "app_123",
        "token": "token_456",
    }

    with patch.object(lambda_handler, "init_credentials"), \
         patch.object(lambda_handler, "ensure_db_available"), \
         patch("lambda_handler.compare_set_vs_parts") as mock_tool, \
         patch("requests.patch") as mock_patch:
        mock_tool.invoke.return_value = "Set vs Parts comparison"
        mock_patch.return_value.status_code = 200

        result = lambda_handler.handle_discord_worker(worker_event)
        assert result["status"] == "success"
        assert result["embed"]["color"] == COLOR_PURPLE


def test_handle_discord_worker_vault_command():
    worker_event = {
        "discord_worker": True,
        "command": "vault",
        "options": {"item": "Rhino Prime"},
        "application_id": "app_123",
        "token": "token_456",
    }

    with patch.object(lambda_handler, "init_credentials"), \
         patch.object(lambda_handler, "ensure_db_available"), \
         patch("lambda_handler.get_vault_status") as mock_tool, \
         patch("requests.patch") as mock_patch:
        mock_tool.invoke.return_value = "Vault details"
        mock_patch.return_value.status_code = 200

        result = lambda_handler.handle_discord_worker(worker_event)
        assert result["status"] == "success"
        assert result["embed"]["color"] == COLOR_YELLOW


def test_handle_discord_worker_ask_command():
    worker_event = {
        "discord_worker": True,
        "command": "ask",
        "options": {"query": "Should I buy Rhino Prime?"},
        "application_id": "app_123",
        "token": "token_456",
    }

    mock_rec = {
        "status": "ok",
        "query": "Should I buy Rhino Prime?",
        "agent_mode": True,
        "response": "Rhino Prime is vaulted, holding is advised.",
    }

    with patch.object(lambda_handler, "init_credentials"), \
         patch.object(lambda_handler, "ensure_db_available"), \
         patch.object(lambda_handler, "get_recommendation", return_value=mock_rec), \
         patch("requests.patch") as mock_patch:
        mock_patch.return_value.status_code = 200

        result = lambda_handler.handle_discord_worker(worker_event)
        assert result["status"] == "success"
        assert "Rhino Prime" in result["embed"]["title"]
        assert "vaulted" in result["embed"]["description"]


def test_lambda_handler_routes_discord_worker_event():
    worker_event = {
        "discord_worker": True,
        "command": "vault",
        "options": {"item": "Rhino Prime"},
    }

    with patch.object(lambda_handler, "handle_discord_worker", return_value={"status": "dispatched"}) as mock_worker:
        resp = lambda_handler.lambda_handler(worker_event)
        assert resp == {"status": "dispatched"}
        mock_worker.assert_called_once_with(worker_event)

"""
tests/test_lambda_handler.py — Unit and integration tests for AWS Lambda execution handler.
Verifies SSM credential fetching, S3 SQLite cache persistence, Discord alert formatting,
and invocation event routing.
"""

import os
import json
import pytest
from unittest.mock import MagicMock, patch
from botocore.exceptions import ClientError

import lambda_handler


@pytest.fixture
def mock_s3_client():
    client = MagicMock()
    return client


@pytest.fixture
def mock_ssm_client():
    client = MagicMock()
    client.get_parameter.return_value = {
        "Parameter": {"Value": "mock_secret_value_123"}
    }
    return client


def test_get_ssm_parameter(mock_ssm_client):
    lambda_handler._SSM_CACHE.clear()
    val = lambda_handler.get_ssm_parameter("/test/param", ssm_client=mock_ssm_client)
    assert val == "mock_secret_value_123"
    mock_ssm_client.get_parameter.assert_called_once_with(
        Name="/test/param", WithDecryption=True
    )

    # Test warm-start memory cache
    val_cached = lambda_handler.get_ssm_parameter("/test/param", ssm_client=mock_ssm_client)
    assert val_cached == "mock_secret_value_123"
    # Should not have called get_parameter again
    assert mock_ssm_client.get_parameter.call_count == 1


def test_ensure_db_available_downloads_from_s3(mock_s3_client, tmp_path):
    test_db = str(tmp_path / "cache" / "wfm.db")
    with patch.object(lambda_handler, "DB_PATH", test_db):
        lambda_handler.ensure_db_available(s3_client=mock_s3_client)
        mock_s3_client.download_file.assert_called_once_with(
            lambda_handler.S3_CACHE_BUCKET, lambda_handler.S3_CACHE_KEY, test_db
        )


def test_ensure_db_available_falls_back_to_seed_on_s3_error(mock_s3_client, tmp_path):
    test_db = str(tmp_path / "cache" / "wfm.db")
    mock_s3_client.download_file.side_effect = ClientError(
        {"Error": {"Code": "404", "Message": "NoSuchKey"}}, "GetObject"
    )

    seed_db = str(tmp_path / "seed.db")
    with open(seed_db, "w") as f:
        f.write("seed data")

    with patch.object(lambda_handler, "DB_PATH", test_db), \
         patch("shutil.copy2") as mock_copy, \
         patch("os.path.exists", side_effect=lambda p: p == seed_db):
        lambda_handler.ensure_db_available(s3_client=mock_s3_client)
        assert mock_s3_client.download_file.call_count == 1


def test_sync_db_to_s3(mock_s3_client, tmp_path):
    test_db = tmp_path / "wfm.db"
    test_db.write_text("dummy database content")

    with patch.object(lambda_handler, "DB_PATH", str(test_db)):
        success = lambda_handler.sync_db_to_s3(s3_client=mock_s3_client)
        assert success is True
        mock_s3_client.upload_file.assert_called_once_with(
            str(test_db), lambda_handler.S3_CACHE_BUCKET, lambda_handler.S3_CACHE_KEY
        )


def test_send_discord_alert_empty_sell_items():
    with patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/test"}), \
         patch("requests.post") as mock_post:
        mock_post.return_value.status_code = 204
        sent = lambda_handler.send_discord_alert([], total_scanned=10)
        assert sent is True
        mock_post.assert_called_once()
        payload = mock_post.call_args[1]["json"]
        assert "All Items Held" in payload["embeds"][0]["title"]


def test_send_discord_alert_with_sell_items():
    sell_items = [{
        "item_name": "Rhino Prime",
        "component_type": "Set",
        "current_price": 120.0,
        "pct_change_90d": 25.5,
        "primary_driver": "vault_status",
        "confidence": "high",
        "reasoning": "Item recently vaulted and supply drying up.",
    }]

    with patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/test"}), \
         patch("requests.post") as mock_post:
        mock_post.return_value.status_code = 204
        sent = lambda_handler.send_discord_alert(sell_items, total_scanned=10)
        assert sent is True
        mock_post.assert_called_once()
        payload = mock_post.call_args[1]["json"]
        assert "SELL Signal(s) Detected" in payload["embeds"][0]["title"]
        assert "Rhino Prime" in payload["embeds"][0]["fields"][0]["name"]


def test_lambda_handler_missing_query_returns_400():
    event = {"queryStringParameters": {}}
    resp = lambda_handler.lambda_handler(event, None)
    assert resp["statusCode"] == 400
    body = json.loads(resp["body"])
    assert "error" in body


def test_lambda_handler_http_query_success():
    event = {"queryStringParameters": {"query": "rhino prime"}}
    mock_result = {
        "status": "resolved",
        "query": "rhino prime",
        "recommendation": "HOLD",
        "reasoning": "Test hold recommendation",
    }

    with patch.object(lambda_handler, "init_credentials"), \
         patch.object(lambda_handler, "ensure_db_available"), \
         patch.object(lambda_handler, "handle_query_request", return_value=mock_result):
        resp = lambda_handler.lambda_handler(event, None)
        assert resp["statusCode"] == 200
        assert resp["headers"]["Access-Control-Allow-Origin"] == "*"
        body = json.loads(resp["body"])
        assert body["recommendation"] == "HOLD"


def test_lambda_handler_scheduled_event_routes_to_batch():
    event = {"mode": "batch"}
    mock_batch_summary = {
        "status": "success",
        "mode": "batch",
        "total_scanned": 17,
        "sell_count": 0,
        "sell_items": [],
    }

    with patch.object(lambda_handler, "init_credentials"), \
         patch.object(lambda_handler, "ensure_db_available"), \
         patch.object(lambda_handler, "run_batch_evaluation", return_value=mock_batch_summary):
        resp = lambda_handler.lambda_handler(event, None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["mode"] == "batch"
        assert body["total_scanned"] == 17

"""
discord_bot/register_commands.py — Register global Discord Slash Commands with the Discord REST API.

Commands:
- /ask  (query: str) - Ask the market advisor any question
- /card (item: str)  - Get full SELL/HOLD recommendation card
- /fair (item: str, price: float) - Check if an offered platinum price is fair
- /parts (item: str) - Compare selling as a full set vs. parts
- /vault (item: str) - Check vault status & Prime Resurgence schedule
"""

import os
import sys
import logging
import requests
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

COMMANDS: List[Dict[str, Any]] = [
    {
        "name": "ask",
        "description": "Ask the market advisor any question",
        "options": [
            {
                "name": "query",
                "description": "Your question about the Warframe market",
                "type": 3,  # STRING
                "required": True,
            }
        ],
    },
    {
        "name": "card",
        "description": "Get full SELL/HOLD recommendation card",
        "options": [
            {
                "name": "item",
                "description": "Name of the Prime item (e.g. Rhino Prime)",
                "type": 3,  # STRING
                "required": True,
            }
        ],
    },
    {
        "name": "fair",
        "description": "Check if an offered platinum price is fair",
        "options": [
            {
                "name": "item",
                "description": "Name of the Prime item (e.g. Rhino Prime)",
                "type": 3,  # STRING
                "required": True,
            },
            {
                "name": "price",
                "description": "Offered platinum price to evaluate",
                "type": 10,  # NUMBER
                "required": True,
            },
        ],
    },
    {
        "name": "parts",
        "description": "Compare selling as a full set vs. parts",
        "options": [
            {
                "name": "item",
                "description": "Name of the Prime item (e.g. Rhino Prime)",
                "type": 3,  # STRING
                "required": True,
            }
        ],
    },
    {
        "name": "vault",
        "description": "Check vault status & Prime Resurgence schedule",
        "options": [
            {
                "name": "item",
                "description": "Name of the Prime item (e.g. Rhino Prime)",
                "type": 3,  # STRING
                "required": True,
            }
        ],
    },
]


def register_commands(application_id: str, bot_token: str) -> List[Dict[str, Any]]:
    """
    Registers the 5 slash commands globally using Discord REST API v10 via POST.

    Args:
        application_id: Discord Application Client ID.
        bot_token: Discord Bot Token for authorization.

    Returns:
        List of response JSON objects from Discord API.
    """
    url = f"https://discord.com/api/v10/applications/{application_id}/commands"
    headers = {
        "Authorization": f"Bot {bot_token}",
        "Content-Type": "application/json",
    }

    results = []
    for cmd in COMMANDS:
        logger.info(f"Registering slash command '/{cmd['name']}'...")
        response = requests.post(url, json=cmd, headers=headers, timeout=10)
        if response.status_code in (200, 201):
            logger.info(f"Successfully registered '/{cmd['name']}'")
            results.append(response.json())
        else:
            logger.error(f"Failed to register '/{cmd['name']}': {response.status_code} {response.text}")
            response.raise_for_status()

    return results


def main() -> None:
    from dotenv import load_dotenv
    load_dotenv()

    application_id = os.getenv("DISCORD_APPLICATION_ID")
    bot_token = os.getenv("DISCORD_BOT_TOKEN")

    if not application_id or not bot_token:
        print("Usage: DISCORD_APPLICATION_ID and DISCORD_BOT_TOKEN must be set.")
        sys.exit(1)

    try:
        registered = register_commands(application_id, bot_token)
        print(f"Successfully registered {len(registered)} commands.")
    except Exception as e:
        print(f"Error registering commands: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

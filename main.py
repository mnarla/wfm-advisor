import os
import sys
import argparse
import logging
import warnings
from dotenv import load_dotenv

# Suppress library warnings (such as model sampling parameter notices)
warnings.filterwarnings("ignore", category=UserWarning)

from ingest.build_db import build_database
from ingest.cache_manager import get_recommendation

# Suppress verbose INFO/DEBUG logs from libraries and internal modules
logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
for logger_name in (
    "httpx",
    "httpcore",
    "google_genai",
    "google",
    "google.genai",
    "urllib3",
    "ingest",
    "nodes",
):
    logging.getLogger(logger_name).setLevel(logging.WARNING)

# Load environment variables
load_dotenv()


def query_item(user_input: str):
    """
    Executes on-demand recommendation query and prints the formatted card.
    Bare item names use the fast sequential pipeline; conversational / intent-specific
    queries are routed to the Phase 2 tool-calling agent.
    """
    print(f"\nProcessing query: '{user_input}'...")
    res = get_recommendation(user_input)

    status = res.get("status")
    if status == "not_found":
        print(f"\n[Error] Item not found for query: '{user_input}'. Please check spelling.")
        return

    if status == "ambiguous":
        candidates = res.get("candidates", [])
        cand_str = ", ".join(candidates)
        print(f"\n[Ambiguous] Multiple candidates found for '{user_input}': {cand_str}")
        print("Please refine your search query (e.g. specify the full frame name).")
        return

    if status == "error":
        print(f"\n[Error] {res.get('response', 'An unknown error occurred.')}")
        return

    formatted_card = res.get("formatted_card")
    if formatted_card:
        # Agent-mode responses don't need the extra card borders
        if res.get("agent_mode"):
            print("\n" + "=" * 80)
            print(formatted_card)
            print("=" * 80 + "\n")
        else:
            print("\n" + formatted_card + "\n")
    else:
        print(res)


def interactive_mode():
    """
    Interactive prompt loop for asking sell-timing advice.
    """
    print("\n================================================================================")
    print("WARFRAME MARKET SELL-TIMING ADVISOR (Phase 2 — Tool-Calling Agent)")
    print()
    print("Examples:")
    print("  rhino prime               → Full SELL/HOLD analysis (fast pipeline)")
    print("  Is 45p fair for Rhino Prime Blueprint?  → Fair-price check")
    print("  Should I buy Loki Prime?  → Buy intent analysis")
    print("  Compare Rhino Prime vs Saryn Prime      → Side-by-side comparison")
    print("  Is Volt Prime vaulted?    → Vault status check")
    print("  Sell Saryn Prime as set or parts?       → Set vs. parts math")
    print("  What changed for Wisp Prime?            → Patch notes analysis")
    print()
    print("Type 'exit' or 'quit' to exit.")
    print("================================================================================\n")

    while True:
        try:
            query = input("Query item > ").strip()
            if not query:
                continue
            if query.lower() in ("exit", "quit", "q"):
                print("Exiting WFM Sell-Timing Advisor. Good luck with your trades!")
                break
            query_item(query)
        except (KeyboardInterrupt, EOFError):
            print("\nExiting.")
            break


def main():
    parser = argparse.ArgumentParser(description="Warframe Market Sell-Timing Advisor CLI")
    parser.add_argument(
        "-q", "--query", type=str, help="Free-text item query (e.g. 'rhino prime neuroptics', 'wisp prime sys')"
    )
    parser.add_argument(
        "-i", "--interactive", action="store_true", help="Start interactive query prompt"
    )
    parser.add_argument(
        "--ingest", action="store_true", help="Fetch watchlist data and rebuild SQLite database"
    )
    parser.add_argument(
        "--batch", action="store_true", help="Run full pipeline batch run on watchlist"
    )
    args = parser.parse_args()

    if args.query:
        query_item(args.query)
        return

    if args.interactive:
        interactive_mode()
        return

    if args.ingest:
        print("Starting data ingestion process...")
        build_database()
        return

    if args.batch:
        from run_pipeline import run_full_pipeline, print_summary_and_full_table
        run_full_pipeline()
        print_summary_and_full_table()
        return

    # Default to interactive mode if no arguments provided
    interactive_mode()


if __name__ == "__main__":
    main()

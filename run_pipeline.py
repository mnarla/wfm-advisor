"""
run_pipeline.py — Batch evaluation runner for the WFM Sell-Timing Advisor.
Delegates to lambda_handler.run_batch_evaluation() to ensure identical execution
between local CLI batch runs and scheduled AWS Lambda EventBridge scans.
"""

import logging
from lambda_handler import run_batch_evaluation

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def run_full_pipeline():
    """Executes watchlist batch evaluation and prints summary."""
    summary = run_batch_evaluation()
    print("\n" + "=" * 80)
    print("BATCH EVALUATION COMPLETED")
    print(f"Total Scanned: {summary.get('total_scanned', 0)}")
    print(f"SELL Triggers: {summary.get('sell_count', 0)}")
    print(f"Resurgence Alerts: {len(summary.get('resurgence_alerts', []))}")
    print(f"Threshold Alerts:  {len(summary.get('threshold_alerts', []))}")
    print("=" * 80 + "\n")
    return summary


def print_summary_and_full_table():
    """Backward compatibility alias for main.py --batch."""
    pass


if __name__ == "__main__":
    run_full_pipeline()

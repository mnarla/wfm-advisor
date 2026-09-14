"""
run_pipeline.py — Batch runner script for the WFM Sell-Timing Advisor pipeline.

Executes the compiled LangGraph pipeline across all 85 items in db/wfm.db,
logging progress per item, capturing errors safely, and printing full results.
"""

import os
import sys
import sqlite3
import logging
from typing import Dict, Any, List

from nodes.graph import create_advisor_graph

DB_PATH = os.getenv("DB_PATH", "db/wfm.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("run_pipeline")


def run_full_pipeline():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Get already processed item_ids
    cur.execute("SELECT DISTINCT item_id FROM recommendations")
    processed_item_ids = set(row[0] for row in cur.fetchall())

    cur.execute(
        """
        SELECT item_id, url_slug, item_name, frame_name, component_type
        FROM items
        ORDER BY frame_name, component_type
        """
    )
    items = [dict(row) for row in cur.fetchall()]
    conn.close()

    total_items = len(items)
    print(f"\n================================================================================")
    print(f"STARTING FULL PIPELINE BATCH RUN ({total_items} items, {len(processed_item_ids)} already stored)")
    print(f"================================================================================\n")

    app = create_advisor_graph()

    success_count = 0
    error_count = 0
    errors_list = []

    for idx, item in enumerate(items, 1):
        item_id = item["item_id"]
        item_name = item["item_name"]
        slug = item["url_slug"]

        if item_id in processed_item_ids:
            print(f"[{idx}/{total_items}] Already processed: {item_name} ({slug}) — skipping.")
            success_count += 1
            continue

        print(f"[{idx}/{total_items}] Processing {item_name} ({slug})...")

        initial_state = {
            "item_id": item_id,
            "url_slug": slug,
            "item_name": item_name,
            "frame_name": item["frame_name"],
            "component_type": item["component_type"],
        }

        try:
            final_state = app.invoke(initial_state)
            rec = final_state.get("recommendation", "UNKNOWN")
            reasoning = final_state.get("reasoning", "")
            print(f"    -> Decision: {rec} | Reasoning preview: {reasoning[:80]}...")
            success_count += 1
        except Exception as e:
            logger.error(f"Error processing item '{slug}' ({item_id}): {e}", exc_info=True)
            error_count += 1
            errors_list.append((slug, str(e)))

        import time
        time.sleep(3.5)

    print(f"\n================================================================================")
    print(f"BATCH RUN FINISHED")
    print(f"  Success: {success_count} / {total_items}")
    print(f"  Errors:  {error_count} / {total_items}")
    print(f"================================================================================\n")

    if errors_list:
        print("ERRORS ENCOUNTERED:")
        for s, err in errors_list:
            print(f"  - {s}: {err}")
        print()


def print_summary_and_full_table():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute(
        """
        SELECT r.id, r.item_id, r.generated_at, r.recommendation, r.confidence, 
               r.primary_driver, r.trend_signal, r.vault_signal, r.patch_signal, r.reasoning,
               i.item_name, i.url_slug, i.frame_name, i.component_type
        FROM recommendations r
        JOIN items i ON r.item_id = i.item_id
        ORDER BY i.frame_name, i.component_type
        """
    )
    rows = [dict(row) for row in cur.fetchall()]
    conn.close()

    total_recs = len(rows)

    # 1. Recommendation counts
    rec_counts = {}
    conf_counts = {}
    driver_counts = {}

    for r in rows:
        rec = r["recommendation"]
        conf = r["confidence"]
        drv = r["primary_driver"]

        rec_counts[rec] = rec_counts.get(rec, 0) + 1
        conf_counts[conf] = conf_counts.get(conf, 0) + 1
        driver_counts[drv] = driver_counts.get(drv, 0) + 1

    print("================================================================================")
    print("PIPELINE SUMMARY METRICS")
    print("================================================================================")
    print(f"Total Recommendations Recorded: {total_recs}")

    print("\nRecommendation Distribution:")
    for k, v in rec_counts.items():
        print(f"  - {k}: {v} ({v/total_recs*100:.1f}%)")

    print("\nConfidence Distribution:")
    for k, v in conf_counts.items():
        print(f"  - {k}: {v} ({v/total_recs*100:.1f}%)")

    print("\nPrimary Driver Distribution:")
    for k, v in driver_counts.items():
        print(f"  - {k}: {v} ({v/total_recs*100:.1f}%)")

    print("\n" + "=" * 140)
    print("FULL RECOMMENDATIONS TABLE (ALL 85 ITEMS)")
    print("=" * 140)

    for i, r in enumerate(rows, 1):
        print(f"\n[{i:02d}/85] {r['item_name']} ({r['url_slug']})")
        print(f"  Recommendation: {r['recommendation']}  |  Confidence: {r['confidence']}  |  Primary Driver: {r['primary_driver']}")
        print(f"  Signals: Trend(slope={r['trend_signal']}) | Vault({r['vault_signal']}) | Patch({r['patch_signal']})")
        print(f"  Reasoning: {r['reasoning']}")


if __name__ == "__main__":
    run_full_pipeline()
    print_summary_and_full_table()

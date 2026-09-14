import sqlite3
import os
import json
import logging
from ingest.fetch_wfm import fetch_watchlist_data

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(os.path.dirname(__file__)), 'db', 'wfm.db'))

def init_db():
    """Initializes the SQLite database with the schema."""
    schema_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'db', 'schema.sql')
    with open(schema_path, 'r') as f:
        schema_sql = f.read()
        
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(schema_sql)
    conn.commit()
    conn.close()

def build_database():
    """
    Orchestrates the fetching, parsing, and writing of WFM data into SQLite.
    """
    init_db()
    
    # Load watchlist
    watchlist_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config', 'watchlist.json')
    with open(watchlist_path, 'r') as f:
        watchlist = json.load(f)
        
    logger.info(f"Starting fetch for {len(watchlist)} frames...")
    
    data = fetch_watchlist_data(watchlist)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    items_inserted = 0
    price_history_inserted = 0
    
    for item in data:
        # 1. Upsert into items table
        cursor.execute('''
            INSERT OR REPLACE INTO items 
            (item_id, url_slug, item_name, frame_name, component_type, vault_status, vault_date, estimated_vault_date)
            VALUES (?, ?, ?, ?, ?, 'unvaulted', NULL, NULL)
        ''', (
            item['item_id'], 
            item['url_slug'], 
            item['item_name'], 
            item['frame_name'], 
            item['component_type']
        ))
        items_inserted += 1
        
        # 2. Insert into price_history table
        for stat_window, stat_key in [('90day', 'price_history_90day'), ('48hr', 'price_history_48hr')]:
            for stat in item.get(stat_key, []):
                cursor.execute('''
                    INSERT OR IGNORE INTO price_history
                    (item_id, recorded_at, avg_price, median_price, volume, moving_avg, stat_window)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    item['item_id'],
                    stat['timestamp'],
                    stat['avg_price'],
                    stat['median_price'],
                    stat['volume'],
                    stat['moving_avg'],
                    stat_window
                ))
                if cursor.rowcount > 0:
                    price_history_inserted += 1
                    
    conn.commit()
    conn.close()
    
    print(f"Database ingestion complete!")
    print(f"Summary:")
    print(f" - {items_inserted} items written/upserted.")
    print(f" - {price_history_inserted} price history rows written.")

if __name__ == '__main__':
    build_database()

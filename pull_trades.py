"""
Polymarket Trade Activity Puller
================================
Pulls all TRADE activity for two wallets from the Polymarket Data API,
then saves a combined JSON file for the dashboard.

Usage:
    pip install requests
    python pull_trades.py

Outputs:
    trades_data.json  — Combined trade data for both wallets
"""

import requests
import json
import time
import os
from datetime import datetime

# ──────────────────────────────────────────────
# CONFIGURATION — edit these as needed
# ──────────────────────────────────────────────

WALLETS = {
    "Sharky": "0x751a2b86cab503496efd325c8344e10159349ea1",
    "Me":     "0xfaf310af70a9a9fa1a43225db317260a2a84f1bc",
}

API_BASE = "https://data-api.polymarket.com"
BATCH_SIZE = 500          # max per request
RATE_LIMIT_DELAY = 0.3    # seconds between requests to be polite
OUTPUT_FILE = "trades_data.json"

# Optional: set to Unix timestamps to limit date range (None = no limit)
START_TIMESTAMP = None    # e.g. 1704067200 for Jan 1 2024
END_TIMESTAMP = None      # e.g. int(time.time()) for now


def fetch_activity(wallet_address: str, label: str) -> list:
    """Fetch all TRADE activity for a wallet, handling pagination."""
    all_trades = []
    offset = 0

    print(f"\n{'='*50}")
    print(f"Fetching trades for {label} ({wallet_address[:10]}...)")
    print(f"{'='*50}")

    while True:
        params = {
            "user": wallet_address,
            "type": "TRADE",
            "limit": BATCH_SIZE,
            "offset": offset,
            "sortBy": "TIMESTAMP",
            "sortDirection": "DESC",
        }

        if START_TIMESTAMP:
            params["start"] = START_TIMESTAMP
        if END_TIMESTAMP:
            params["end"] = END_TIMESTAMP

        try:
            resp = requests.get(f"{API_BASE}/activity", params=params, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  ERROR at offset {offset}: {e}")
            break

        batch = resp.json()
        if not batch:
            break

        all_trades.extend(batch)
        print(f"  Fetched {len(batch)} trades (total: {len(all_trades)}, offset: {offset})")

        if len(batch) < BATCH_SIZE:
            break  # last page

        offset += BATCH_SIZE
        time.sleep(RATE_LIMIT_DELAY)

    print(f"  DONE — {len(all_trades)} total trades for {label}")
    return all_trades


def normalize_trade(trade: dict, wallet_label: str) -> dict:
    """Normalize a trade record into a clean, flat structure."""
    ts = trade.get("timestamp", 0)
    dt = datetime.utcfromtimestamp(ts) if ts else None

    return {
        "wallet": wallet_label,
        "timestamp": ts,
        "datetime": dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None,
        "date": dt.strftime("%Y-%m-%d") if dt else None,
        "hour": dt.hour if dt else None,
        "day_of_week": dt.strftime("%A") if dt else None,
        "side": trade.get("side", ""),
        "slug": trade.get("slug", ""),
        "event_slug": trade.get("eventSlug", ""),
        "title": trade.get("title", ""),
        "outcome": trade.get("outcome", ""),
        "size": trade.get("size", 0),           # shares
        "usdc_size": trade.get("usdcSize", 0),  # dollar amount
        "price": trade.get("price", 0),          # price per share
        "name": trade.get("name", ""),
        "pseudonym": trade.get("pseudonym", ""),
        "transaction_hash": trade.get("transactionHash", ""),
        "condition_id": trade.get("conditionId", ""),
        "proxy_wallet": trade.get("proxyWallet", ""),
    }


def main():
    print("Polymarket Trade Activity Puller")
    print(f"Pulling data for {len(WALLETS)} wallets...\n")

    all_normalized = []

    for label, address in WALLETS.items():
        raw_trades = fetch_activity(address, label)
        for t in raw_trades:
            all_normalized.append(normalize_trade(t, label))

    # Sort by timestamp descending
    all_normalized.sort(key=lambda x: x["timestamp"], reverse=True)

    # Summary stats
    print(f"\n{'='*50}")
    print("SUMMARY")
    print(f"{'='*50}")
    for label in WALLETS:
        count = sum(1 for t in all_normalized if t["wallet"] == label)
        total_usdc = sum(t["usdc_size"] for t in all_normalized if t["wallet"] == label)
        print(f"  {label}: {count} trades, ${total_usdc:,.2f} total volume")

    # Find overlapping markets
    markets_by_wallet = {}
    for label in WALLETS:
        markets_by_wallet[label] = set(
            t["slug"] for t in all_normalized if t["wallet"] == label
        )
    overlap = set.intersection(*markets_by_wallet.values()) if len(markets_by_wallet) > 1 else set()
    print(f"\n  Overlapping markets: {len(overlap)}")
    if overlap:
        for slug in list(overlap)[:10]:
            print(f"    - {slug}")
        if len(overlap) > 10:
            print(f"    ... and {len(overlap) - 10} more")

    # Save
    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), OUTPUT_FILE)
    with open(output_path, "w") as f:
        json.dump({
            "generated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
            "wallets": {label: addr for label, addr in WALLETS.items()},
            "total_trades": len(all_normalized),
            "trades": all_normalized,
        }, f, indent=2)

    print(f"\nData saved to: {output_path}")
    print(f"Total records: {len(all_normalized)}")
    print("\nNow open the dashboard HTML file in your browser!")


if __name__ == "__main__":
    main()

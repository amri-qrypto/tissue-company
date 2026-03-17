#!/usr/bin/env python3
"""
Sharky Strategy Bot — Polymarket Trading Bot
=============================================
Mimics Sharky's trading strategy on Polymarket "Up or Down" crypto markets.

Strategy:
- Finds all active "Up or Down" markets for BTC, ETH, XRP, SOL
- Places $10 market-buy orders 2 seconds before each market closes
- Buys the YES outcome (Up) — following Sharky's observed pattern
- Runs continuously, scanning for upcoming closes

Requirements:
    pip install py-clob-client requests python-dotenv

Setup:
    1. Copy .env.example to .env
    2. Fill in your private key and wallet address
    3. Run: python sharky_bot.py
"""

import os
import re
import sys
import json
import time
import logging
import hashlib
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ══════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════

# Try loading .env file
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Core Settings ──
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0"))  # 0=EOA, 1=Magic, 2=Proxy

# ── Strategy Parameters ──
TRADE_AMOUNT_USD = float(os.getenv("TRADE_AMOUNT_USD", "10.0"))     # $10 per trade
SECONDS_BEFORE_CLOSE = float(os.getenv("SECONDS_BEFORE_CLOSE", "2"))  # 2 seconds before close
ASSETS = os.getenv("ASSETS", "BTC,ETH,XRP,SOL").upper().split(",")   # All crypto assets

# ── API Endpoints ──
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CHAIN_ID = 137  # Polygon

# ── Operational ──
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "30"))  # seconds between market scans
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"  # Default to dry run for safety
LOG_FILE = os.getenv("LOG_FILE", "sharky_bot.log")
TRADE_LOG_FILE = os.getenv("TRADE_LOG_FILE", "trades.json")

# ══════════════════════════════════════════════════════
# LOGGING SETUP
# ══════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("sharky_bot")

# ══════════════════════════════════════════════════════
# ASSET KEYWORDS — maps asset tickers to search terms
# ══════════════════════════════════════════════════════

ASSET_KEYWORDS = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "eth"],
    "XRP": ["xrp", "ripple"],
    "SOL": ["solana", "sol"],
    "DOGE": ["doge", "dogecoin"],
    "BNB": ["bnb", "binance"],
    "ADA": ["ada", "cardano"],
    "MATIC": ["matic", "polygon"],
    "AVAX": ["avax", "avalanche"],
    "LINK": ["link", "chainlink"],
    "DOT": ["dot", "polkadot"],
    "HYPE": ["hype", "hyperliquid"],
    "SUI": ["sui"],
    "TON": ["ton", "toncoin"],
}

# ══════════════════════════════════════════════════════
# MARKET CLOSE TIME PARSER (ported from dashboard.html)
# ══════════════════════════════════════════════════════

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "september": 9, "oct": 10, "october": 10,
    "nov": 11, "november": 11, "dec": 12, "december": 12,
}

TZ_OFFSETS = {
    "ET": -4, "EST": -5, "EDT": -4,
    "CT": -5, "CST": -6, "CDT": -5,
    "PT": -7, "PST": -8, "PDT": -7,
    "MT": -6, "MST": -7, "MDT": -6,
    "UTC": 0,
}


def parse_time_ampm(h_str, m_str, ampm):
    """Parse hour:minute AM/PM to 24h format."""
    hour = int(h_str)
    minute = int(m_str) if m_str else 0
    ampm = ampm.upper()
    if ampm == "PM" and hour != 12:
        hour += 12
    if ampm == "AM" and hour == 12:
        hour = 0
    return hour, minute


def parse_market_close_time(title):
    """
    Parse market title to extract close time as Unix timestamp (UTC).
    Supports multiple format patterns from Polymarket market titles.
    Returns None if close time cannot be determined.
    """
    if not title:
        return None

    now = datetime.now(timezone.utc)
    year = now.year

    # Determine market type for close hour
    is_stock = bool(re.search(
        r'\b(MSFT|AAPL|TSLA|AMZN|GOOG|GOOGL|META|NVDA|AMD|NFLX|DIS|BABA|JPM|BAC|GS|V|MA|WMT|KO|PEP|NKE|BA|CAT|XOM|CVX|PFE|JNJ|UNH|SPY|QQQ|IWM|VTI)\b',
        title, re.IGNORECASE
    ))
    is_above = bool(re.search(r'\babove\b', title, re.IGNORECASE))
    is_price = bool(re.search(r'\bprice\b', title, re.IGNORECASE))

    def get_close_hour_et():
        if is_stock:
            return 16  # 4 PM ET
        if is_above:
            return 12  # 12 PM ET (noon)
        if is_price:
            return 12  # 12 PM ET (noon)
        return 0  # 12 AM ET (midnight) — default for reach/dip/hit

    # Pattern 1: RANGE format "March 15, 11:00AM-11:15AM ET"
    range_match = re.search(
        r'(\w+)\s+(\d{1,2}),?\s+\d{1,2}(?::\d{2})?\s*(?:AM|PM)\s*-\s*(\d{1,2})(?::(\d{2}))?\s*(AM|PM)\s*(ET|EST|EDT|CT|CST|CDT|PT|PST|PDT|MT|MST|MDT|UTC)',
        title, re.IGNORECASE
    )
    if range_match:
        month_str = range_match.group(1).lower()
        month = MONTHS.get(month_str)
        if month is not None:
            day = int(range_match.group(2))
            hour, minute = parse_time_ampm(range_match.group(3), range_match.group(4), range_match.group(5))
            tz = range_match.group(6).upper()
            tz_off = TZ_OFFSETS.get(tz, -4)
            dt = datetime(year, month, day, hour, minute, 0, tzinfo=timezone(timedelta(hours=tz_off)))
            return int(dt.timestamp())

    # Pattern 2: SINGLE time "March 15, 10AM ET" or "March 15, 10:30AM ET"
    single_match = re.search(
        r'(\w+)\s+(\d{1,2}),?\s+(\d{1,2})(?::(\d{2}))?\s*(AM|PM)\s*(ET|EST|EDT|CT|CST|CDT|PT|PST|PDT|MT|MST|MDT|UTC)',
        title, re.IGNORECASE
    )
    if single_match:
        month_str = single_match.group(1).lower()
        month = MONTHS.get(month_str)
        if month is not None:
            day = int(single_match.group(2))
            hour, minute = parse_time_ampm(single_match.group(3), single_match.group(4), single_match.group(5))
            tz = single_match.group(6).upper()
            tz_off = TZ_OFFSETS.get(tz, -4)
            dt = datetime(year, month, day, hour, minute, 0, tzinfo=timezone(timedelta(hours=tz_off)))
            return int(dt.timestamp())

    # Pattern 3: DATE RANGE "March 9-15?"
    date_range_match = re.search(
        r'(\w+)\s+(\d{1,2})\s*-\s*(\d{1,2})(?:\s*,?\s*(\d{4}))?\s*\?',
        title, re.IGNORECASE
    )
    if date_range_match:
        month_str = date_range_match.group(1).lower()
        month = MONTHS.get(month_str)
        if month is not None:
            end_day = int(date_range_match.group(3))
            yr = int(date_range_match.group(4)) if date_range_match.group(4) else year
            close_h = get_close_hour_et()
            tz_off = -4  # ET
            if close_h == 0:
                # Midnight = start of next day
                dt = datetime(yr, month, end_day + 1, 0, 0, 0, tzinfo=timezone(timedelta(hours=tz_off)))
            else:
                dt = datetime(yr, month, end_day, close_h, 0, 0, tzinfo=timezone(timedelta(hours=tz_off)))
            return int(dt.timestamp())

    # Pattern 4: SINGLE DATE "on March 16?"
    single_date_match = re.search(
        r'(?:on|by)\s+(\w+)\s+(\d{1,2})(?:\s*,?\s*(\d{4}))?\s*\?',
        title, re.IGNORECASE
    )
    if single_date_match:
        month_str = single_date_match.group(1).lower()
        month = MONTHS.get(month_str)
        if month is not None:
            day = int(single_date_match.group(2))
            yr = int(single_date_match.group(3)) if single_date_match.group(3) else year
            close_h = get_close_hour_et()
            tz_off = -4  # ET
            if close_h == 0:
                dt = datetime(yr, month, day + 1, 0, 0, 0, tzinfo=timezone(timedelta(hours=tz_off)))
            else:
                dt = datetime(yr, month, day, close_h, 0, 0, tzinfo=timezone(timedelta(hours=tz_off)))
            return int(dt.timestamp())

    # Pattern 5: "end of March?"
    eom_match = re.search(r'end of\s+(\w+)(?:\s*,?\s*(\d{4}))?\s*\?', title, re.IGNORECASE)
    if eom_match:
        month_str = eom_match.group(1).lower()
        month = MONTHS.get(month_str)
        if month is not None:
            yr = int(eom_match.group(2)) if eom_match.group(2) else year
            tz_off = -4  # ET
            close_h = get_close_hour_et()
            # Last day of month
            if month == 12:
                last_day = 31
            else:
                last_day = (datetime(yr, month + 1, 1) - timedelta(days=1)).day
            if close_h == 0:
                dt = datetime(yr, month, last_day + 1, 0, 0, 0, tzinfo=timezone(timedelta(hours=tz_off)))
            else:
                dt = datetime(yr, month, last_day, close_h, 0, 0, tzinfo=timezone(timedelta(hours=tz_off)))
            return int(dt.timestamp())

    # Pattern 6: "in March?" — monthly, closes midnight ET 1st of next month
    monthly_match = re.search(r'in\s+(\w+)(?:\s*,?\s*(\d{4}))?\s*\?', title, re.IGNORECASE)
    if monthly_match:
        month_str = monthly_match.group(1).lower()
        month = MONTHS.get(month_str)
        if month is not None:
            yr = int(monthly_match.group(2)) if monthly_match.group(2) else year
            tz_off = -4  # ET
            # Midnight ET on 1st of next month
            next_month = month + 1
            next_yr = yr
            if next_month > 12:
                next_month = 1
                next_yr += 1
            dt = datetime(next_yr, next_month, 1, 0, 0, 0, tzinfo=timezone(timedelta(hours=tz_off)))
            return int(dt.timestamp())

    return None


def is_up_or_down_market(title):
    """Check if a market title indicates an 'Up or Down' crypto market."""
    return bool(re.search(r'up or down', title, re.IGNORECASE))


def get_market_asset(title):
    """Extract the crypto asset ticker from a market title."""
    title_lower = title.lower()
    for ticker, keywords in ASSET_KEYWORDS.items():
        for kw in keywords:
            if kw in title_lower:
                return ticker
    return None


# ══════════════════════════════════════════════════════
# GAMMA API — Market Discovery
# ══════════════════════════════════════════════════════

def fetch_active_markets(limit=100, offset=0):
    """Fetch active markets from Polymarket's Gamma API."""
    try:
        resp = requests.get(
            f"{GAMMA_API}/markets",
            params={
                "active": "true",
                "closed": "false",
                "limit": limit,
                "offset": offset,
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.error(f"Failed to fetch markets: {e}")
        return []


def fetch_all_active_markets():
    """Fetch all active markets with pagination."""
    all_markets = []
    offset = 0
    limit = 100
    while True:
        batch = fetch_active_markets(limit=limit, offset=offset)
        if not batch:
            break
        all_markets.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
        time.sleep(0.3)  # Rate limiting
    return all_markets


def find_target_markets():
    """
    Find all active 'Up or Down' crypto markets for our target assets.
    Returns list of market dicts with parsed close times.
    """
    all_markets = fetch_all_active_markets()
    log.info(f"Fetched {len(all_markets)} active markets from Gamma API")

    targets = []
    for m in all_markets:
        question = m.get("question", "") or m.get("title", "") or ""
        if not is_up_or_down_market(question):
            continue

        asset = get_market_asset(question)
        if asset not in ASSETS:
            continue

        close_ts = parse_market_close_time(question)
        if close_ts is None:
            log.warning(f"Cannot parse close time for: {question}")
            continue

        now_ts = int(time.time())
        if close_ts <= now_ts:
            continue  # Already closed

        # Extract token IDs from the market
        tokens = m.get("tokens", [])
        condition_id = m.get("conditionId", "")
        slug = m.get("slug", "")

        # Find YES (Up) token
        yes_token = None
        no_token = None
        for tok in tokens:
            outcome = (tok.get("outcome", "") or "").lower()
            if outcome in ("yes", "up"):
                yes_token = tok.get("token_id", "")
            elif outcome in ("no", "down"):
                no_token = tok.get("token_id", "")

        if not yes_token:
            # Fallback: first token = YES, second = NO
            if len(tokens) >= 1:
                yes_token = tokens[0].get("token_id", "")
            if len(tokens) >= 2:
                no_token = tokens[1].get("token_id", "")

        targets.append({
            "question": question,
            "asset": asset,
            "close_ts": close_ts,
            "close_dt": datetime.fromtimestamp(close_ts, tz=timezone.utc).isoformat(),
            "condition_id": condition_id,
            "slug": slug,
            "yes_token": yes_token,
            "no_token": no_token,
            "market_id": m.get("id", ""),
            "seconds_until_close": close_ts - now_ts,
        })

    targets.sort(key=lambda x: x["close_ts"])
    log.info(f"Found {len(targets)} target Up/Down markets for {ASSETS}")
    return targets


# ══════════════════════════════════════════════════════
# CLOB CLIENT — Trade Execution
# ══════════════════════════════════════════════════════

_clob_client = None


def get_clob_client():
    """Initialize and return the py-clob-client instance."""
    global _clob_client
    if _clob_client is not None:
        return _clob_client

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        if not PRIVATE_KEY:
            log.error("POLYMARKET_PRIVATE_KEY not set. Cannot initialize CLOB client.")
            return None

        client = ClobClient(
            CLOB_HOST,
            key=PRIVATE_KEY,
            chain_id=CHAIN_ID,
            signature_type=SIGNATURE_TYPE,
            funder=FUNDER_ADDRESS if FUNDER_ADDRESS else None,
        )

        # Create or derive API credentials
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)

        log.info("CLOB client initialized successfully")
        _clob_client = client
        return client

    except ImportError:
        log.error("py-clob-client not installed. Run: pip install py-clob-client")
        return None
    except Exception as e:
        log.error(f"Failed to initialize CLOB client: {e}")
        return None


def get_best_price(client, token_id, side="BUY"):
    """Get the current best price for a token."""
    try:
        price = client.get_price(token_id, side=side)
        return float(price) if price else None
    except Exception as e:
        log.warning(f"Could not get price for {token_id}: {e}")
        return None


def place_market_order(token_id, amount_usd, market_info):
    """
    Place a market BUY order for the YES outcome.
    Returns order response or None on failure.
    """
    if DRY_RUN:
        log.info(
            f"[DRY RUN] Would BUY ${amount_usd} of YES on: {market_info['question']}"
            f" | Token: {token_id[:16]}... | Asset: {market_info['asset']}"
        )
        return {"dry_run": True, "amount": amount_usd, "token_id": token_id}

    client = get_clob_client()
    if not client:
        log.error("Cannot place order: CLOB client not available")
        return None

    try:
        from py_clob_client.clob_types import MarketOrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        # Place a FOK (Fill-or-Kill) market order for $amount_usd
        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=amount_usd,
            side=BUY,
            order_type=OrderType.FOK,
        )

        signed_order = client.create_market_order(order_args)
        resp = client.post_order(signed_order, OrderType.FOK)

        log.info(
            f"ORDER PLACED: BUY ${amount_usd} YES on {market_info['asset']} "
            f"| Market: {market_info['question'][:60]}... "
            f"| Response: {resp}"
        )
        return resp

    except Exception as e:
        log.error(f"ORDER FAILED: {e} | Market: {market_info['question'][:60]}...")
        return None


# ══════════════════════════════════════════════════════
# TRADE LOGGER — Persistent trade history
# ══════════════════════════════════════════════════════

def log_trade(market_info, result, amount_usd):
    """Append trade to the JSON trade log file."""
    trade_record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "unix_ts": int(time.time()),
        "asset": market_info["asset"],
        "question": market_info["question"],
        "close_ts": market_info["close_ts"],
        "close_dt": market_info["close_dt"],
        "slug": market_info["slug"],
        "condition_id": market_info["condition_id"],
        "token_id": market_info.get("yes_token", ""),
        "amount_usd": amount_usd,
        "side": "BUY",
        "outcome": "YES",
        "dry_run": DRY_RUN,
        "result": str(result)[:500] if result else "None",
    }

    log_path = Path(TRADE_LOG_FILE)
    trades = []
    if log_path.exists():
        try:
            trades = json.loads(log_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, IOError):
            trades = []

    trades.append(trade_record)
    log_path.write_text(json.dumps(trades, indent=2), encoding="utf-8")
    log.info(f"Trade logged: {market_info['asset']} ${amount_usd} → {TRADE_LOG_FILE}")


# ══════════════════════════════════════════════════════
# SCHEDULER — Waits and fires trades at precise times
# ══════════════════════════════════════════════════════

# Track which markets we've already traded to avoid duplicates
traded_markets = set()


def get_market_key(market):
    """Generate a unique key for a market to track duplicates."""
    return hashlib.md5(
        f"{market['condition_id']}:{market['close_ts']}".encode()
    ).hexdigest()


def execute_trade_at_close(market):
    """Wait until SECONDS_BEFORE_CLOSE before market close, then trade."""
    market_key = get_market_key(market)
    if market_key in traded_markets:
        return

    now_ts = time.time()
    trade_ts = market.get("close_ts", 0) - SECONDS_BEFORE_CLOSE
    wait_seconds = trade_ts - now_ts

    if wait_seconds < -10:
        # Already past the window
        log.debug(f"Skipping (past window): {market['question'][:60]}...")
        return

    if wait_seconds > 0:
        log.info(
            f"⏳ Waiting {wait_seconds:.1f}s for: {market['asset']} "
            f"| Close: {market['close_dt']} "
            f"| Market: {market['question'][:60]}..."
        )
        # Sleep in small chunks so we can be interrupted
        end_time = time.time() + wait_seconds
        while time.time() < end_time:
            remaining = end_time - time.time()
            time.sleep(min(remaining, 1.0))

    # ── FIRE! ──
    token_id = market.get("yes_token", "")
    if not token_id:
        log.error(f"No YES token for: {market['question'][:60]}...")
        return

    log.info(
        f"🔥 TRADING NOW: BUY ${TRADE_AMOUNT_USD} YES on {market['asset']} "
        f"| {market['question'][:60]}..."
    )

    result = place_market_order(token_id, TRADE_AMOUNT_USD, market)
    log_trade(market, result, TRADE_AMOUNT_USD)
    traded_markets.add(market_key)


# ══════════════════════════════════════════════════════
# MAIN LOOP
# ══════════════════════════════════════════════════════

def print_banner():
    """Print startup banner."""
    print("""
╔═══════════════════════════════════════════════════════╗
║           🦈 SHARKY STRATEGY BOT v1.0 🦈              ║
║    Polymarket "Up or Down" Crypto Trading Bot         ║
╠═══════════════════════════════════════════════════════╣
║  Strategy: BUY YES (Up) 2s before market close       ║
║  Amount:   $%.2f per trade                          ║
║  Assets:   %-40s ║
║  Mode:     %-40s ║
╚═══════════════════════════════════════════════════════╝
""" % (
        TRADE_AMOUNT_USD,
        ", ".join(ASSETS),
        "🔴 DRY RUN (no real trades)" if DRY_RUN else "🟢 LIVE TRADING",
    ))


def run_scan_cycle():
    """
    Single scan cycle:
    1. Fetch all active Up/Down markets
    2. Find markets closing within the next SCAN_INTERVAL + buffer
    3. Execute trades for upcoming closes
    """
    targets = find_target_markets()
    now_ts = time.time()

    # Focus on markets closing within the next scan window (+ generous buffer)
    window = SCAN_INTERVAL + 60  # seconds
    upcoming = [
        m for m in targets
        if 0 < (m["close_ts"] - now_ts) <= window
    ]

    if upcoming:
        log.info(f"📊 {len(upcoming)} market(s) closing within {window}s window:")
        for m in upcoming:
            secs = m["close_ts"] - now_ts
            log.info(
                f"   → {m['asset']:4s} | Closes in {secs:.0f}s "
                f"| {m['question'][:55]}..."
            )

    # Execute trades for each upcoming market
    for m in upcoming:
        execute_trade_at_close(m)

    # Also log markets further out for visibility
    future = [m for m in targets if (m["close_ts"] - now_ts) > window]
    if future:
        next_m = future[0]
        next_secs = next_m["close_ts"] - now_ts
        log.info(
            f"Next market: {next_m['asset']} in {next_secs:.0f}s "
            f"({next_m['close_dt']}) — {next_m['question'][:50]}..."
        )


def main():
    """Main entry point — continuous scanning loop."""
    print_banner()

    # Validate configuration
    if not DRY_RUN and not PRIVATE_KEY:
        log.error("❌ POLYMARKET_PRIVATE_KEY required for live trading. Set DRY_RUN=true or provide key.")
        sys.exit(1)

    if not DRY_RUN:
        client = get_clob_client()
        if not client:
            log.error("❌ Could not initialize CLOB client. Check your credentials.")
            sys.exit(1)

    log.info(f"Starting bot — scanning every {SCAN_INTERVAL}s")
    log.info(f"Target assets: {', '.join(ASSETS)}")
    log.info(f"Trade amount: ${TRADE_AMOUNT_USD}")
    log.info(f"Entry timing: {SECONDS_BEFORE_CLOSE}s before close")
    log.info(f"Mode: {'DRY RUN' if DRY_RUN else 'LIVE'}")
    log.info("=" * 55)

    try:
        while True:
            try:
                run_scan_cycle()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                log.error(f"Scan cycle error: {e}")

            log.info(f"💤 Sleeping {SCAN_INTERVAL}s until next scan...")
            time.sleep(SCAN_INTERVAL)

    except KeyboardInterrupt:
        log.info("\n🛑 Bot stopped by user (Ctrl+C)")
        print(f"\nTrades logged to: {TRADE_LOG_FILE}")
        print(f"Full log at: {LOG_FILE}")


# ══════════════════════════════════════════════════════
# CLI COMMANDS
# ══════════════════════════════════════════════════════

def cmd_scan():
    """One-off scan: show all upcoming Up/Down markets."""
    targets = find_target_markets()
    if not targets:
        print("No active Up/Down markets found for target assets.")
        return

    print(f"\n{'Asset':<6} {'Closes In':<12} {'Close Time (UTC)':<22} {'Market'}")
    print("─" * 100)
    now_ts = time.time()
    for m in targets:
        secs = m["close_ts"] - now_ts
        if secs > 0:
            if secs < 60:
                closes_in = f"{secs:.0f}s"
            elif secs < 3600:
                closes_in = f"{secs/60:.1f}m"
            elif secs < 86400:
                closes_in = f"{secs/3600:.1f}h"
            else:
                closes_in = f"{secs/86400:.1f}d"
        else:
            closes_in = "PAST"

        print(f"{m['asset']:<6} {closes_in:<12} {m['close_dt']:<22} {m['question'][:55]}")

    print(f"\nTotal: {len(targets)} markets | Assets: {', '.join(ASSETS)}")


def cmd_test_parser():
    """Test the close time parser with example market titles."""
    test_titles = [
        "Bitcoin Up or Down - March 16, 10:00AM-10:15AM ET",
        "Bitcoin Up or Down - March 16, 7PM ET",
        "Ethereum Up or Down - March 16, 11:00AM-12:00PM ET",
        "Will Bitcoin reach $74,000 on March 16?",
        "Will the price of Bitcoin be above $86,000 on February 10?",
        "Will Bitcoin reach $74,000 March 9-15?",
        "Will Bitcoin dip to $60,000 in March?",
        "Will Microsoft (MSFT) close above $330 end of March?",
        "XRP Up or Down - March 16, 3:00PM-4:00PM ET",
        "Solana Up or Down - March 17, 9:00AM-9:05AM ET",
    ]

    print(f"\n{'Market Title':<60} {'Close Time (UTC)':<22} {'Seconds'}")
    print("─" * 100)
    now_ts = time.time()
    for title in test_titles:
        close_ts = parse_market_close_time(title)
        if close_ts:
            dt = datetime.fromtimestamp(close_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            secs = close_ts - now_ts
            print(f"{title[:58]:<60} {dt:<22} {secs:.0f}s")
        else:
            print(f"{title[:58]:<60} {'UNPARSEABLE':<22}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()
        if cmd == "scan":
            cmd_scan()
        elif cmd == "test":
            cmd_test_parser()
        elif cmd == "run":
            main()
        else:
            print("Usage: python sharky_bot.py [run|scan|test]")
            print("  run   — Start the bot (continuous)")
            print("  scan  — Show upcoming markets (one-shot)")
            print("  test  — Test the close-time parser")
    else:
        main()

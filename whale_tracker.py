"""
Politician Whale Tracker
========================
Fetches Congressional stock trades from:
  - housestockwatcher.com  (House of Representatives)
  - capitoltrades.com      (House + Senate combined)
  - senatestockwatcher.com (Senate)

Scores every politician by actual profitability (checks stock price
30 days after each trade using yfinance).

Ranks the top whales and generates copy-trade signals.
"""

import os
import json
import time
import logging
import requests
from datetime import datetime, date, timedelta
from typing import Optional
import yfinance as yf
import pandas as pd

log = logging.getLogger("WhaleTracker")

# =============================================================================
#  CONFIG
# =============================================================================
DATA_DIR              = os.getenv("DATA_DIR", "/app/data")
RANKINGS_FILE         = os.path.join(DATA_DIR, "whale_rankings.json")
TRADES_CACHE_FILE     = os.path.join(DATA_DIR, "politician_trades_cache.json")

WHALE_TOP_N           = 5      # copy trades from top N politicians
WHALE_MIN_TRADES      = 5      # minimum trades needed to qualify for ranking
WHALE_LOOKBACK_DAYS   = 365    # score trades from last 12 months
WHALE_RETURN_DAYS     = 30     # measure stock return 30 days after trade
WHALE_CACHE_TTL       = 3600   # refresh trade data every hour
RANKINGS_REFRESH_DAYS = 1      # re-score whales every day

_trades_cache      = []
_trades_cache_time = 0
_rankings_cache    = []
_rankings_time     = 0


# =============================================================================
#  DATA FETCHING
# =============================================================================
def fetch_house_trades() -> list:
    """House trades from housestockwatcher.com (free JSON API)."""
    try:
        url  = "https://housestockwatcher.com/api"
        resp = requests.get(url, timeout=20,
                            headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        data = resp.json()
        trades = []
        for t in data:
            ticker = (t.get("ticker") or "").strip().upper()
            if not ticker or ticker in ("--", "N/A", ""):
                continue
            trades.append({
                "politician": t.get("representative", "Unknown"),
                "chamber":    "House",
                "ticker":     ticker,
                "tx_date":    t.get("transaction_date", "")[:10],
                "tx_type":    t.get("type", "").lower(),
                "amount":     t.get("amount", ""),
                "source":     "housestockwatcher",
            })
        log.info("House trades fetched: %d records", len(trades))
        return trades
    except Exception as e:
        log.warning("fetch_house_trades failed: %s", e)
        return []


def fetch_senate_trades() -> list:
    """Senate trades from senatestockwatcher.com."""
    try:
        url  = "https://senatestockwatcher.com/api"
        resp = requests.get(url, timeout=20,
                            headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        data = resp.json()
        trades = []
        for t in data:
            ticker = (t.get("ticker") or "").strip().upper()
            if not ticker or ticker in ("--", "N/A", ""):
                continue
            trades.append({
                "politician": t.get("senator", "Unknown"),
                "chamber":    "Senate",
                "ticker":     ticker,
                "tx_date":    t.get("transaction_date", "")[:10],
                "tx_type":    t.get("type", "").lower(),
                "amount":     t.get("amount", ""),
                "source":     "senatestockwatcher",
            })
        log.info("Senate trades fetched: %d records", len(trades))
        return trades
    except Exception as e:
        log.warning("fetch_senate_trades failed: %s", e)
        return []


def fetch_capitol_trades() -> list:
    """
    Capitol Trades (capitoltrades.com) — covers both House and Senate.
    Uses their public data endpoint.
    """
    try:
        # Capitol Trades public API
        url  = "https://www.capitoltrades.com/api/trades"
        params = {
            "pageSize": 500,
            "page":     1,
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept":     "application/json",
            "Referer":    "https://www.capitoltrades.com/trades",
        }
        resp = requests.get(url, params=params, headers=headers, timeout=20)

        if resp.status_code != 200:
            log.warning("Capitol Trades API returned %d", resp.status_code)
            return []

        data = resp.json()
        items = data if isinstance(data, list) else data.get("data", data.get("trades", []))
        trades = []

        for t in items:
            # Handle different response structures
            ticker = (
                t.get("ticker") or
                t.get("asset", {}).get("ticker") or
                t.get("instrument", {}).get("ticker") or ""
            ).strip().upper()

            if not ticker or ticker in ("--", "N/A", ""):
                continue

            politician = (
                t.get("politician", {}).get("name") or
                t.get("politicianName") or
                t.get("representative") or
                "Unknown"
            )

            tx_date = (
                t.get("txDate") or
                t.get("transactionDate") or
                t.get("transaction_date") or ""
            )[:10]

            tx_type = (
                t.get("txType") or
                t.get("type") or
                t.get("transactionType") or ""
            ).lower()

            chamber = (
                t.get("chamber") or
                t.get("politician", {}).get("chamber") or
                "Unknown"
            )

            trades.append({
                "politician": politician,
                "chamber":    chamber,
                "ticker":     ticker,
                "tx_date":    tx_date,
                "tx_type":    tx_type,
                "amount":     t.get("amount", ""),
                "source":     "capitoltrades",
            })

        log.info("Capitol Trades fetched: %d records", len(trades))
        return trades

    except Exception as e:
        log.warning("fetch_capitol_trades failed: %s", e)
        return []


def get_all_trades(force_refresh: bool = False) -> list:
    """
    Combine all sources, deduplicate, and cache results.
    Returns unified list of politician trades.
    """
    global _trades_cache, _trades_cache_time

    now = time.time()
    if not force_refresh and _trades_cache and (now - _trades_cache_time) < WHALE_CACHE_TTL:
        return _trades_cache

    # Try loading from file first
    try:
        with open(TRADES_CACHE_FILE) as f:
            cached = json.load(f)
        cache_age = now - cached.get("timestamp", 0)
        if not force_refresh and cache_age < WHALE_CACHE_TTL:
            _trades_cache      = cached["trades"]
            _trades_cache_time = cached["timestamp"]
            return _trades_cache
    except Exception:
        pass

    # Fetch from all sources
    log.info("Fetching politician trades from all sources...")
    house   = fetch_house_trades()
    senate  = fetch_senate_trades()
    capitol = fetch_capitol_trades()

    # Combine and deduplicate
    all_trades = house + senate + capitol
    seen       = set()
    unique     = []
    for t in all_trades:
        key = f"{t['politician']}|{t['ticker']}|{t['tx_date']}|{t['tx_type']}"
        if key not in seen:
            seen.add(key)
            unique.append(t)

    # Filter to lookback window
    cutoff = date.today() - timedelta(days=WHALE_LOOKBACK_DAYS)
    unique = [t for t in unique if t.get("tx_date", "") >= cutoff.isoformat()]
    unique.sort(key=lambda t: t.get("tx_date", ""), reverse=True)

    _trades_cache      = unique
    _trades_cache_time = now

    # Save to file
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(TRADES_CACHE_FILE, "w") as f:
            json.dump({"timestamp": now, "trades": unique}, f)
    except Exception as e:
        log.warning("Could not save trades cache: %s", e)

    log.info("Total unique politician trades: %d", len(unique))
    return unique


# =============================================================================
#  POLITICIAN SCORING
# =============================================================================
def score_trade(ticker: str, tx_date_str: str, tx_type: str) -> Optional[float]:
    """
    Returns % return WHALE_RETURN_DAYS days after the trade date.
    Positive = stock went up after buy, or down after sell.
    """
    try:
        tx_date  = datetime.strptime(tx_date_str, "%Y-%m-%d").date()
        end_date = tx_date + timedelta(days=WHALE_RETURN_DAYS + 5)

        # Don't score trades that are too recent (not enough data yet)
        if (date.today() - tx_date).days < WHALE_RETURN_DAYS:
            return None

        tkr  = yf.Ticker(ticker)
        hist = tkr.history(start=tx_date_str,
                           end=end_date.isoformat(),
                           interval="1d",
                           auto_adjust=True)

        if hist is None or len(hist) < 2:
            return None

        price_entry = float(hist["Close"].iloc[0])
        price_exit  = float(hist["Close"].iloc[min(WHALE_RETURN_DAYS, len(hist) - 1)])

        if price_entry <= 0:
            return None

        pct_change = (price_exit - price_entry) / price_entry * 100

        # For sells/puts: profit if stock went DOWN
        if "sale" in tx_type or "sell" in tx_type or "put" in tx_type:
            return -pct_change  # flip sign: negative stock move = profit for seller
        else:
            return pct_change   # buy/call: profit if stock went UP

    except Exception as e:
        log.debug("score_trade %s %s failed: %s", ticker, tx_date_str, e)
        return None


def build_whale_rankings(all_trades: list) -> list:
    """
    Score every politician by their historical trade performance.
    Returns list sorted by profitability (best first).
    """
    log.info("Building whale rankings from %d trades...", len(all_trades))

    # Group trades by politician
    by_politician = {}
    for t in all_trades:
        name = t["politician"]
        if name not in by_politician:
            by_politician[name] = []
        by_politician[name].append(t)

    rankings = []
    for name, trades in by_politician.items():
        if len(trades) < WHALE_MIN_TRADES:
            continue

        scores  = []
        scored_trades = []
        for t in trades:
            score = score_trade(t["ticker"], t["tx_date"], t["tx_type"])
            if score is not None:
                scores.append(score)
                scored_trades.append({**t, "score": round(score, 2)})

        if len(scores) < WHALE_MIN_TRADES:
            continue

        avg_return  = sum(scores) / len(scores)
        win_rate    = len([s for s in scores if s > 0]) / len(scores) * 100
        total_pnl   = sum(scores)
        best_trade  = max(scored_trades, key=lambda x: x["score"])
        worst_trade = min(scored_trades, key=lambda x: x["score"])

        # Composite score: avg return * win rate
        composite = avg_return * (win_rate / 100)

        rankings.append({
            "politician":   name,
            "chamber":      trades[0].get("chamber", "Unknown"),
            "total_trades": len(trades),
            "scored_trades":len(scores),
            "avg_return":   round(avg_return, 2),
            "win_rate":     round(win_rate, 1),
            "total_return": round(total_pnl, 2),
            "composite":    round(composite, 2),
            "best_trade":   best_trade,
            "worst_trade":  worst_trade,
            "recent_trades": sorted(trades, key=lambda x: x["tx_date"], reverse=True)[:5],
        })

    rankings.sort(key=lambda x: x["composite"], reverse=True)
    log.info("Ranked %d politicians — top whale: %s (%.1f%% win rate, %.2f%% avg return)",
             len(rankings),
             rankings[0]["politician"] if rankings else "N/A",
             rankings[0]["win_rate"]   if rankings else 0,
             rankings[0]["avg_return"] if rankings else 0)

    return rankings


def get_whale_rankings(force_refresh: bool = False) -> list:
    """
    Returns cached whale rankings, refreshing daily.
    """
    global _rankings_cache, _rankings_time

    now = time.time()

    # Try loading from file
    try:
        with open(RANKINGS_FILE) as f:
            cached = json.load(f)
        age_days = (now - cached.get("timestamp", 0)) / 86400
        if not force_refresh and age_days < RANKINGS_REFRESH_DAYS:
            _rankings_cache = cached["rankings"]
            return _rankings_cache
    except Exception:
        pass

    # Build fresh rankings
    all_trades = get_all_trades(force_refresh=force_refresh)
    rankings   = build_whale_rankings(all_trades)

    _rankings_cache = rankings
    _rankings_time  = now

    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(RANKINGS_FILE, "w") as f:
            json.dump({"timestamp": now, "rankings": rankings}, f, indent=2)
    except Exception as e:
        log.warning("Could not save rankings: %s", e)

    return rankings


# =============================================================================
#  COPY TRADE SIGNALS
# =============================================================================
def get_whale_signals(lookback_days: int = 3) -> list:
    """
    Get recent trades from top WHALE_TOP_N politicians.
    Returns list of dicts: {symbol, signal, politician, date, amount, rank}

    signal = 'CALL' for buys, 'PUT' for sells
    """
    rankings = get_whale_rankings()
    if not rankings:
        return []

    top_whales = {r["politician"] for r in rankings[:WHALE_TOP_N]}
    all_trades = get_all_trades()
    cutoff     = date.today() - timedelta(days=lookback_days)

    signals = []
    seen    = set()

    for t in all_trades:
        if t["politician"] not in top_whales:
            continue
        try:
            tx_date = datetime.strptime(t["tx_date"], "%Y-%m-%d").date()
        except Exception:
            continue
        if tx_date < cutoff:
            continue

        ticker  = t["ticker"]
        tx_type = t["tx_type"].lower()
        key     = f"{ticker}|{tx_type}|{t['tx_date']}"
        if key in seen:
            continue
        seen.add(key)

        # Determine direction
        if any(word in tx_type for word in ("purchase", "buy", "call")):
            signal = "CALL"
        elif any(word in tx_type for word in ("sale", "sell", "put")):
            signal = "PUT"
        else:
            continue

        # Find politician's rank
        rank = next((i + 1 for i, r in enumerate(rankings)
                     if r["politician"] == t["politician"]), 99)

        signals.append({
            "symbol":      ticker,
            "signal":      signal,
            "politician":  t["politician"],
            "chamber":     t.get("chamber", ""),
            "date":        t["tx_date"],
            "amount":      t.get("amount", ""),
            "rank":        rank,
            "source":      t.get("source", ""),
        })

    # Sort by rank (best whale first) then by date (newest first)
    signals.sort(key=lambda x: (x["rank"], x["date"]))
    log.info("Whale signals: %d from top %d politicians", len(signals), WHALE_TOP_N)
    return signals


# =============================================================================
#  LEADERBOARD SUMMARY
# =============================================================================
def get_leaderboard_text(top_n: int = 10) -> str:
    """Returns a formatted leaderboard string for Telegram."""
    rankings = get_whale_rankings()
    if not rankings:
        return "No whale rankings available yet — still scoring trades..."

    lines = f"🏛️ Politician Whale Leaderboard\n{'─'*30}\n"
    for i, r in enumerate(rankings[:top_n], 1):
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"#{i}"
        lines += (
            f"{medal} {r['politician']} ({r['chamber']})\n"
            f"   Win rate: {r['win_rate']:.0f}%  "
            f"Avg return: {r['avg_return']:+.1f}%  "
            f"Trades: {r['scored_trades']}\n"
        )
    lines += f"{'─'*30}\n"
    lines += f"Copying top {WHALE_TOP_N}: {', '.join(r['politician'].split()[-1] for r in rankings[:WHALE_TOP_N])}"
    return lines


def get_recent_whale_activity_text() -> str:
    """Returns recent whale trades formatted for Telegram."""
    signals = get_whale_signals(lookback_days=7)
    if not signals:
        return "No whale trades in the last 7 days."

    lines = f"🐋 Recent Whale Activity (7 days)\n{'─'*30}\n"
    for s in signals[:15]:
        emoji = "📈" if s["signal"] == "CALL" else "📉"
        lines += (
            f"{emoji} {s['symbol']} — {s['signal']}\n"
            f"   {s['politician']} (#{s['rank']}) • {s['date']}\n"
            f"   Amount: {s['amount']} • {s['source']}\n"
        )
    return lines

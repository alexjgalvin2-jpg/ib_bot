"""
Risk Filters — shared by Tradier and IB bots
============================================
1. VIX Filter       — adjusts position size based on market fear level
2. Earnings Blackout — skips stocks within 5 days of earnings (IV crush risk)
3. Sector Correlation — limits how many positions per sector at once
"""

import logging
import time
from datetime import datetime, date, timedelta
from typing import Optional
import yfinance as yf

log = logging.getLogger("RiskFilters")

# =============================================================================
#  VIX FILTER
# =============================================================================
VIX_LOW       = 20.0   # below this = cheap options, trade normally
VIX_HIGH      = 30.0   # above this = expensive options, reduce/stop buying
VIX_CACHE_TTL = 900    # refresh VIX every 15 minutes

_vix_cache     = None
_vix_cache_time = 0


def get_vix() -> float:
    """Returns current VIX level. Cached for 15 minutes."""
    global _vix_cache, _vix_cache_time
    now = time.time()
    if _vix_cache is not None and (now - _vix_cache_time) < VIX_CACHE_TTL:
        return _vix_cache
    try:
        hist = yf.Ticker("^VIX").history(period="2d", interval="1d")
        if hist is not None and not hist.empty:
            vix = float(hist["Close"].iloc[-1])
            _vix_cache      = vix
            _vix_cache_time = now
            log.info("VIX: %.1f", vix)
            return vix
    except Exception as e:
        log.warning("VIX fetch failed: %s", e)
    return _vix_cache if _vix_cache else 20.0  # safe default


def get_vix_regime() -> str:
    """
    Returns the current market regime based on VIX:
      'low'    → VIX < 20, options cheap, trade normally
      'medium' → VIX 20-30, options expensive, reduce size by 50%
      'high'   → VIX > 30, do NOT buy options, wheel/sell premium only
    """
    vix = get_vix()
    if vix < VIX_LOW:
        return "low"
    elif vix < VIX_HIGH:
        return "medium"
    else:
        return "high"


def vix_premium_multiplier() -> float:
    """
    Returns a multiplier for MAX_PREMIUM based on VIX:
      low    → 1.0  (full size)
      medium → 0.5  (half size)
      high   → 0.0  (don't buy long options)
    """
    regime = get_vix_regime()
    if regime == "low":
        return 1.0
    elif regime == "medium":
        log.info("⚠️ VIX elevated — reducing position size by 50%%")
        return 0.5
    else:
        log.warning("🚨 VIX > 30 — long options too expensive, skipping entry")
        return 0.0


def vix_status_text() -> str:
    vix    = get_vix()
    regime = get_vix_regime()
    emoji  = "🟢" if regime == "low" else "🟡" if regime == "medium" else "🔴"
    advice = {
        "low":    "Cheap options — full size trading",
        "medium": "Elevated fear — half size trading",
        "high":   "High fear — wheel strategy only",
    }
    return f"{emoji} VIX: {vix:.1f} ({advice[regime]})"


# =============================================================================
#  EARNINGS BLACKOUT
# =============================================================================
EARNINGS_BLACKOUT_DAYS = 5   # skip if earnings within this many days
_earnings_cache = {}          # symbol → (earnings_date, cache_timestamp)
EARNINGS_CACHE_TTL = 86400    # refresh earnings dates once per day


def get_next_earnings(symbol: str) -> Optional[date]:
    """
    Returns the next earnings date for a symbol, or None if unknown.
    Cached per symbol for 24 hours.
    """
    now = time.time()
    if symbol in _earnings_cache:
        cached_date, cached_time = _earnings_cache[symbol]
        if (now - cached_time) < EARNINGS_CACHE_TTL:
            return cached_date

    try:
        tkr = yf.Ticker(symbol)

        # Try earnings_dates (newer yfinance)
        try:
            ed = tkr.earnings_dates
            if ed is not None and not ed.empty:
                today = date.today()
                future = [d.date() for d in ed.index if d.date() >= today]
                if future:
                    result = min(future)
                    _earnings_cache[symbol] = (result, now)
                    return result
        except Exception:
            pass

        # Try calendar (older yfinance)
        try:
            cal = tkr.calendar
            if cal is not None and not cal.empty:
                earnings_date = cal.columns[0]
                if hasattr(earnings_date, 'date'):
                    result = earnings_date.date()
                    _earnings_cache[symbol] = (result, now)
                    return result
        except Exception:
            pass

    except Exception as e:
        log.debug("get_next_earnings %s: %s", symbol, e)

    _earnings_cache[symbol] = (None, now)
    return None


def is_earnings_blackout(symbol: str) -> bool:
    """
    Returns True if the stock has earnings within EARNINGS_BLACKOUT_DAYS.
    We skip these to avoid IV crush after earnings.
    """
    earnings = get_next_earnings(symbol)
    if earnings is None:
        return False

    today     = date.today()
    days_away = (earnings - today).days

    if 0 <= days_away <= EARNINGS_BLACKOUT_DAYS:
        log.info("🚫 %s earnings in %d days (%s) — skipping to avoid IV crush",
                 symbol, days_away, earnings)
        return True
    return False


def earnings_status_text(symbol: str) -> str:
    earnings = get_next_earnings(symbol)
    if earnings is None:
        return f"{symbol}: earnings date unknown"
    days_away = (earnings - date.today()).days
    emoji = "🚫" if days_away <= EARNINGS_BLACKOUT_DAYS else "✅"
    return f"{emoji} {symbol}: earnings in {days_away} days ({earnings})"


# =============================================================================
#  SECTOR CORRELATION LIMIT
# =============================================================================
SECTORS = {
    "tech":     ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMD",
                 "INTC", "QCOM", "ARM",  "SMCI"],
    "growth":   ["TSLA", "SHOP", "COIN", "PLTR", "CRWD", "HOOD",
                 "SOFI", "MSTR", "RBLX", "DKNG", "SNAP"],
    "consumer": ["NFLX", "UBER", "AMZN"],
    "etf":      ["SPY",  "QQQ",  "IWM",  "ARKK", "SOXL"],
    "finance":  ["JPM",  "GS",   "BAC",  "MS"],
    "energy":   ["XOM",  "CVX",  "OXY"],
    "pharma":   ["LLY",  "MRNA", "BNTX"],
    "space":    ["RKLB", "ASTS", "BA",   "LMT"],
    "crypto":   ["COIN", "MSTR", "HOOD"],
}

MAX_SECTOR_POSITIONS = 3  # max open positions per sector at once


def get_sector(symbol: str) -> str:
    """Returns the sector for a given symbol."""
    for sector, symbols in SECTORS.items():
        if symbol in symbols:
            return sector
    return "other"


def sector_at_limit(symbol: str, positions: dict) -> bool:
    """
    Returns True if we already have MAX_SECTOR_POSITIONS open
    in the same sector as this symbol.
    """
    sector      = get_sector(symbol)
    sector_count = sum(
        1 for pos in positions.values()
        if get_sector(pos.get("symbol", "")) == sector
    )
    if sector_count >= MAX_SECTOR_POSITIONS:
        log.info("📊 %s sector '%s' at limit (%d/%d) — skipping",
                 symbol, sector, sector_count, MAX_SECTOR_POSITIONS)
        return True
    return False


def sector_exposure_text(positions: dict) -> str:
    """Returns a summary of current sector exposure."""
    counts = {}
    for pos in positions.values():
        sector = get_sector(pos.get("symbol", "other"))
        counts[sector] = counts.get(sector, 0) + 1

    if not counts:
        return "No open positions"

    lines = ""
    for sector, count in sorted(counts.items(), key=lambda x: -x[1]):
        bar   = "█" * count
        warn  = " ⚠️" if count >= MAX_SECTOR_POSITIONS else ""
        lines += f"  {sector}: {bar} {count}{warn}\n"
    return lines


# =============================================================================
#  COMBINED RISK CHECK
# =============================================================================
def passes_all_filters(symbol: str, positions: dict) -> tuple:
    """
    Run all three risk filters. Returns (passed: bool, reason: str).
    Use this as a single gate before entering any trade.
    """
    # 1. VIX check
    multiplier = vix_premium_multiplier()
    if multiplier == 0.0:
        return False, f"VIX too high ({get_vix():.1f}) — not buying options"

    # 2. Earnings blackout
    if is_earnings_blackout(symbol):
        earnings = get_next_earnings(symbol)
        return False, f"Earnings blackout ({earnings})"

    # 3. Sector limit
    if sector_at_limit(symbol, positions):
        sector = get_sector(symbol)
        return False, f"Sector '{sector}' at limit ({MAX_SECTOR_POSITIONS} positions)"

    return True, "ok"

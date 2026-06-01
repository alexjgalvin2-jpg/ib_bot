"""
Interactive Brokers Options Bot — Client Portal REST API
Runs on Railway 24/7 — no local IB Gateway needed.

Uses IB Client Portal API for trading.
Uses yfinance for signal detection and option chain data.
"""

import os
import time
import json
import logging
import requests
import urllib3
from datetime import datetime, date, timedelta
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import numpy as np
import yfinance as yf

# Suppress SSL warnings for localhost gateway
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# =============================================================================
#  CONFIG
# =============================================================================
IB_GATEWAY_URL = os.getenv("IB_GATEWAY_URL", "https://localhost:5000")
IB_USERNAME    = os.getenv("IB_USERNAME", "")
IB_PASSWORD    = os.getenv("IB_PASSWORD", "")
IB_ACCOUNT     = os.getenv("IB_ACCOUNT", "")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "8775367667:AAEdG8sT9G4BQJpcG-YhTlYTxqGgy66ZUK4")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "8911522385")

PAPER_MODE = True

DATA_DIR       = os.getenv("DATA_DIR", "/app/data")
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE     = os.path.join(DATA_DIR, "ib_state.json")
TRADE_LOG_FILE = os.path.join(DATA_DIR, "ib_trades.json")

MAX_PREMIUM      = 500.0
MAX_POSITIONS    = 50
TAKE_PROFIT_PCT  = 25.0
STOP_LOSS_PCT    = 20.0
SCAN_INTERVAL    = 300
DAILY_LOSS_LIMIT = 5000.0
SYMBOL_COOLDOWN_HOURS = 24
DAILY_SUMMARY_HOUR = 16

TARGET_DTE_MIN   = 25
TARGET_DTE_MAX   = 50
TARGET_DELTA_MIN = 0.30
TARGET_DELTA_MAX = 0.55

RSI_PERIOD  = 14
RSI_BULL    = 55
RSI_BEAR    = 45
SMA_PERIOD  = 20
VOLUME_MULT = 1.3
SIGNAL_MODE = "all"
STRADDLE_MODE = True
PARALLEL_SCAN = True

SYMBOLS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
    "AMD",  "ARM",  "SMCI", "INTC", "QCOM",
    "COIN", "PLTR", "CRWD", "SHOP", "MSTR", "SOFI",
    "NFLX", "UBER", "SNAP", "HOOD", "RBLX", "DKNG",
    "SPY",  "QQQ",  "IWM",  "ARKK",
    "JPM",  "GS",   "BAC",
    "XOM",  "CVX",
    "LLY",  "MRNA",
]
SYMBOLS = list(dict.fromkeys(SYMBOLS))

PLATFORM = "📊 Interactive Brokers"

# =============================================================================
#  LOGGING
# =============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("ib_bot.log", encoding="utf-8"),
    ]
)
log = logging.getLogger("IBRestBot")


# =============================================================================
#  TELEGRAM
# =============================================================================
def send_telegram(msg: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": f"[{PLATFORM}]\n{msg}"
        }, timeout=10)
    except Exception as e:
        log.warning("Telegram failed: %s", e)


_last_update_id = 0

def check_telegram_commands(bot) -> None:
    global _last_update_id
    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        resp = requests.get(url, params={"offset": _last_update_id + 1, "timeout": 2}, timeout=10)
        data = resp.json()
        for update in data.get("result", []):
            _last_update_id = update["update_id"]
            text = update.get("message", {}).get("text", "").strip().lower()
            if text == "/report":
                _send_report(bot)
            elif text == "/accounthistory":
                _send_history()
    except Exception as e:
        log.debug("check_telegram_commands failed: %s", e)


def _send_report(bot):
    try:
        positions = bot.state.get("positions", {})
        trade_log = _load_trades()
        closes    = [t for t in trade_log if t.get("action") == "close"]
        winners   = [t for t in closes if t.get("pnl_usd", 0) > 0]
        losers    = [t for t in closes if t.get("pnl_usd", 0) <= 0]
        total_pnl = sum(t.get("pnl_usd", 0) for t in closes)
        win_rate  = (len(winners) / len(closes) * 100) if closes else 0
        best  = max(closes, key=lambda t: t.get("pnl_usd", 0)) if closes else None
        worst = min(closes, key=lambda t: t.get("pnl_usd", 0)) if closes else None

        pos_lines = ""
        for k, pos in positions.items():
            pos_lines += f"  • {pos['symbol']} {pos['option_type']} ${pos['strike']} exp {pos['expiration']}\n"

        send_telegram(
            f"📊 IB Report\n"
            f"Mode: PAPER 🧪\n"
            f"─────────────────\n"
            f"Open positions: {len(positions)}/{MAX_POSITIONS}\n"
            f"{pos_lines if pos_lines else '  None\n'}"
            f"─────────────────\n"
            f"Total closed: {len(closes)}\n"
            f"Winners: {len(winners)}  Losers: {len(losers)}\n"
            f"Win rate: {win_rate:.0f}%\n"
            f"Total P&L: ${total_pnl:+.2f}\n"
            + (f"Best: {best['symbol']} ${best['pnl_usd']:+.2f}\n" if best else "")
            + (f"Worst: {worst['symbol']} ${worst['pnl_usd']:+.2f}" if worst else "No trades yet")
        )
    except Exception as e:
        send_telegram(f"⚠️ Report error: {e}")


def _send_history():
    try:
        trades  = _load_trades()
        closes  = [t for t in trades if t.get("action") == "close"]
        if not closes:
            send_telegram("📜 IB Account History\nNo closed trades yet.")
            return
        running = 0.0
        chunk   = f"📜 IB Account History\n{'─'*25}\n"
        messages = []
        for i, t in enumerate(closes, 1):
            pnl = t.get("pnl_usd", 0)
            running += pnl
            ts    = t.get("timestamp", "")[:10]
            emoji = "✅" if pnl > 0 else "❌"
            line  = f"{emoji} #{i} {ts} | {t.get('symbol','?')} {t.get('option_type','?')} | P&L: ${pnl:+.2f} | Running: ${running:+.2f}\n"
            if len(chunk) + len(line) > 3800:
                messages.append(chunk)
                chunk = f"📜 History (cont.)\n{'─'*25}\n"
            chunk += line
        chunk += f"{'─'*25}\nFinal P&L: ${running:+.2f}"
        messages.append(chunk)
        for m in messages:
            send_telegram(m)
    except Exception as e:
        send_telegram(f"⚠️ History error: {e}")


# =============================================================================
#  IB CLIENT PORTAL API
# =============================================================================
class IBClient:
    def __init__(self):
        self.base    = IB_GATEWAY_URL
        self.session = requests.Session()
        self.session.verify = False  # self-signed cert on gateway
        self.account = IB_ACCOUNT

    def _get(self, path: str, params: dict = None) -> Optional[dict]:
        try:
            r = self.session.get(f"{self.base}{path}", params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning("GET %s failed: %s", path, e)
            return None

    def _post(self, path: str, body: dict = None) -> Optional[dict]:
        try:
            r = self.session.post(f"{self.base}{path}", json=body, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning("POST %s failed: %s", path, e)
            return None

    def auth_status(self) -> bool:
        data = self._get("/v1/api/iserver/auth/status")
        return bool(data and data.get("authenticated"))

    def keepalive(self):
        self._post("/v1/api/tickle")

    def get_accounts(self) -> list:
        data = self._get("/v1/api/iserver/accounts")
        if data:
            accounts = data.get("accounts", [])
            if accounts and not self.account:
                self.account = accounts[0]
            return accounts
        return []

    def get_net_liq(self) -> float:
        if not self.account:
            return 0.0
        data = self._get(f"/v1/api/portfolio/{self.account}/summary")
        if data:
            nl = data.get("netliquidation", {})
            return float(nl.get("amount", 0))
        return 0.0

    def search_contract(self, symbol: str, sec_type: str = "OPT") -> Optional[int]:
        data = self._post("/v1/api/iserver/secdef/search", {
            "symbol": symbol, "secType": sec_type
        })
        if data and isinstance(data, list) and data:
            return data[0].get("conid")
        return None

    def place_order(self, conid: int, action: str, quantity: int) -> Optional[dict]:
        if PAPER_MODE:
            log.info("📄 PAPER ORDER: %s conid=%d x%d", action, conid, quantity)
            return {"orderId": f"paper-{int(time.time())}", "paper": True}

        body = {
            "orders": [{
                "conid":     conid,
                "orderType": "MKT",
                "side":      action,
                "quantity":  quantity,
                "tif":       "GTC",
            }]
        }
        data = self._post(f"/v1/api/iserver/account/{self.account}/orders", body)
        return data


# =============================================================================
#  TECHNICAL SIGNALS
# =============================================================================
def compute_rsi(series: pd.Series, period: int = 14) -> float:
    delta = series.diff().dropna()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_g = gain.ewm(com=period - 1, min_periods=period).mean().iloc[-1]
    avg_l = loss.ewm(com=period - 1, min_periods=period).mean().iloc[-1]
    if avg_l == 0:
        return 100.0
    return 100 - (100 / (1 + avg_g / avg_l))


def get_signal(symbol: str, rsi_adj: int = 0) -> Optional[str]:
    try:
        df = yf.download(symbol, period="3mo", interval="1d",
                         progress=False, auto_adjust=True)
        if df is None or len(df) < SMA_PERIOD + 5:
            return None
        close     = df["Close"].squeeze()
        volume    = df["Volume"].squeeze()
        sma       = close.rolling(SMA_PERIOD).mean().iloc[-1]
        price     = close.iloc[-1]
        rsi       = compute_rsi(close, RSI_PERIOD)
        vol_ma    = volume.rolling(10).mean().iloc[-2]
        vol_now   = volume.iloc[-1]
        vol_ratio = vol_now / vol_ma if vol_ma > 0 else 0

        rsi_bull = RSI_BULL + rsi_adj
        rsi_bear = RSI_BEAR - rsi_adj

        log.info("%s  price=%.2f  SMA=%.2f  RSI=%.1f  vol=%.2fx",
                 symbol, price, sma, rsi, vol_ratio)

        spike = vol_ratio >= VOLUME_MULT
        if price > sma and rsi >= rsi_bull and spike:
            return "CALL"
        if price < sma and rsi <= rsi_bear and spike:
            return "PUT"
        return None
    except Exception as e:
        log.warning("get_signal %s: %s", symbol, e)
        return None


def pick_option(symbol: str, right: str,
                spot: float) -> Optional[tuple]:
    """Use yfinance to find best option. Returns (exp_str, strike, ask) or None."""
    try:
        tkr   = yf.Ticker(symbol)
        today = date.today()
        valid_exps = []
        for exp_str in tkr.options:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            dte      = (exp_date - today).days
            if TARGET_DTE_MIN <= dte <= TARGET_DTE_MAX:
                valid_exps.append((dte, exp_str))
        if not valid_exps:
            return None
        valid_exps.sort(key=lambda x: x[0])
        _, exp_str = valid_exps[0]

        chain = tkr.option_chain(exp_str)
        opts  = chain.calls if right == "C" else chain.puts

        candidates = []
        for _, row in opts.iterrows():
            ask   = row.get("ask", 0) or 0
            delta = abs(row.get("delta") or 0)
            if ask <= 0 or ask * 100 > MAX_PREMIUM:
                continue
            if TARGET_DELTA_MIN <= delta <= TARGET_DELTA_MAX:
                candidates.append((abs(delta - 0.40), row))

        if candidates:
            candidates.sort(key=lambda x: x[0])
            best = candidates[0][1]
        else:
            opts_f = opts[(opts["ask"] > 0) & (opts["ask"] * 100 <= MAX_PREMIUM)]
            if opts_f.empty:
                return None
            idx  = (opts_f["strike"] - spot).abs().idxmin()
            best = opts_f.loc[idx]

        return (exp_str, float(best["strike"]), float(best["ask"]))
    except Exception as e:
        log.warning("pick_option %s: %s", symbol, e)
        return None


# =============================================================================
#  STATE & TRADE LOG
# =============================================================================
def _load_trades() -> list:
    try:
        with open(TRADE_LOG_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"positions": {}}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def log_trade(entry: dict):
    try:
        trades = _load_trades()
        trades.append(entry)
        with open(TRADE_LOG_FILE, "w") as f:
            json.dump(trades, f, indent=2)
    except Exception as e:
        log.warning("log_trade failed: %s", e)


# =============================================================================
#  MAIN BOT
# =============================================================================
class IBRestBot:
    def __init__(self):
        self.client          = IBClient()
        self.state           = load_state()
        self.scan_count      = 0
        self.signal_queue    = {}
        self.market_fired    = False
        self.trading_halted  = False
        self.symbol_cooldowns = {}
        self.last_summary_day = None
        self.daily_trades    = []

        try:
            trades = _load_trades()
            self.alltime_pnl = sum(t.get("pnl_usd", 0) for t in trades
                                   if t.get("action") == "close")
        except Exception:
            self.alltime_pnl = 0.0

        log.info("IBRestBot initialised — PAPER mode")

    def _is_market_open(self) -> bool:
        now = datetime.now()
        if now.weekday() >= 5:
            return False
        open_t  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
        close_t = now.replace(hour=16, minute=0,  second=0, microsecond=0)
        return open_t <= now <= close_t

    def _today_pnl(self) -> float:
        today = date.today().isoformat()
        trades = _load_trades()
        return sum(t.get("pnl_usd", 0) for t in trades
                   if t.get("action") == "close" and
                   t.get("timestamp", "").startswith(today))

    def _check_daily_loss(self) -> bool:
        if self._today_pnl() <= -DAILY_LOSS_LIMIT:
            if not self.trading_halted:
                self.trading_halted = True
                send_telegram(
                    f"🚨 DAILY LOSS LIMIT HIT\n"
                    f"Today's P&L: ${self._today_pnl():+.2f}\n"
                    f"Limit: -${DAILY_LOSS_LIMIT:.0f}\n"
                    f"Trading HALTED until tomorrow."
                )
            return True
        return False

    def _on_cooldown(self, symbol: str) -> bool:
        if symbol in self.symbol_cooldowns:
            elapsed = (datetime.now() - self.symbol_cooldowns[symbol]).total_seconds() / 3600
            if elapsed < SYMBOL_COOLDOWN_HOURS:
                return True
            del self.symbol_cooldowns[symbol]
        return False

    def _check_exits(self):
        positions = self.state.get("positions", {})
        to_close  = []
        for key, pos in list(positions.items()):
            try:
                right   = "C" if pos["option_type"] == "CALL" else "P"
                exp_str = pos["expiration"]
                tkr     = yf.Ticker(pos["symbol"])
                chain   = tkr.option_chain(exp_str)
                opts    = chain.calls if right == "C" else chain.puts
                row     = opts[opts["strike"] == pos["strike"]]
                if row.empty:
                    continue
                price   = float(row.iloc[0].get("lastPrice") or
                                row.iloc[0].get("bid") or 0)
                if price <= 0:
                    continue
                entry   = pos["entry_price"]
                pnl_pct = (price - entry) / entry * 100
                if pnl_pct >= TAKE_PROFIT_PCT:
                    to_close.append((key, pos, price, pnl_pct, "TAKE PROFIT ✅"))
                elif pnl_pct <= -STOP_LOSS_PCT:
                    to_close.append((key, pos, price, pnl_pct, "STOP LOSS ❌"))
            except Exception as e:
                log.warning("Exit check %s: %s", key, e)

        for key, pos, price, pnl_pct, reason in to_close:
            pnl_usd = (price - pos["entry_price"]) * pos["quantity"] * 100
            self.alltime_pnl += pnl_usd
            entry = {
                "action":      "close",
                "reason":      reason,
                "symbol":      pos["symbol"],
                "option_type": pos["option_type"],
                "strike":      pos["strike"],
                "expiration":  pos["expiration"],
                "entry_price": pos["entry_price"],
                "exit_price":  price,
                "pnl_pct":     round(pnl_pct, 2),
                "pnl_usd":     round(pnl_usd, 2),
                "timestamp":   datetime.now().isoformat(),
            }
            log_trade(entry)
            self.daily_trades.append(entry)

            if pnl_usd < 0:
                self.symbol_cooldowns[pos["symbol"]] = datetime.now()

            send_telegram(
                f"{'📄 PAPER ' if PAPER_MODE else ''}OPTIONS CLOSE — {reason}\n"
                f"Symbol: {pos['symbol']} {pos['option_type']} ${pos['strike']}\n"
                f"Entry: ${pos['entry_price']:.2f}  Exit: ${price:.2f}\n"
                f"P&L: {pnl_pct:+.1f}% (${pnl_usd:+.2f})\n"
                f"All-time P&L: ${self.alltime_pnl:+.2f}"
            )
            del self.state["positions"][key]
            save_state(self.state)
            log.info("Closed %s — %s P&L: %+.1f%%", key, reason, pnl_pct)

    def _try_entry(self, symbol: str, signal: str):
        if self._check_daily_loss():
            return
        if self._on_cooldown(symbol):
            return

        positions = self.state.get("positions", {})
        for pos in positions.values():
            if pos["symbol"] == symbol and pos["option_type"] == signal:
                return
        if len(positions) >= MAX_POSITIONS:
            return

        # Get spot price
        try:
            tkr   = yf.Ticker(symbol)
            hist  = tkr.history(period="1d", interval="1m")
            spot  = float(hist["Close"].iloc[-1]) if not hist.empty else None
        except Exception:
            spot = None
        if not spot:
            return

        right  = "C" if signal == "CALL" else "P"
        result = pick_option(symbol, right, spot)
        if not result:
            return

        exp_str, strike, ask = result
        if not ask or ask <= 0:
            return

        quantity = max(1, int(MAX_PREMIUM / (ask * 100)))
        cost     = ask * quantity * 100

        # Paper mode — log without real order
        order_result = self.client.place_order(0, "BUY", quantity)
        if not order_result:
            return

        key = f"{symbol}_{signal}_{exp_str}_{strike}"
        self.state.setdefault("positions", {})[key] = {
            "symbol":      symbol,
            "option_type": signal,
            "strike":      strike,
            "expiration":  exp_str,
            "entry_price": ask,
            "quantity":    quantity,
            "timestamp":   datetime.now().isoformat(),
        }
        save_state(self.state)

        dte = (datetime.strptime(exp_str, "%Y-%m-%d").date() - date.today()).days
        send_telegram(
            f"{'📄 PAPER ' if PAPER_MODE else ''}OPTIONS ENTRY\n"
            f"Symbol: {symbol}  Signal: {signal}\n"
            f"Strike: ${strike}  Exp: {exp_str} ({dte} DTE)\n"
            f"Ask: ${ask:.2f}  Qty: {quantity}  Cost: ${cost:.2f}\n"
            f"TP: +{TAKE_PROFIT_PCT}%  SL: -{STOP_LOSS_PCT}%"
        )
        entry = {
            "action":      "open",
            "symbol":      symbol,
            "option_type": signal,
            "strike":      strike,
            "expiration":  exp_str,
            "entry_price": ask,
            "quantity":    quantity,
            "cost":        round(cost, 2),
            "timestamp":   datetime.now().isoformat(),
        }
        log_trade(entry)
        self.daily_trades.append(entry)
        log.info("Entered %s %s strike=%.0f @ $%.2f x%d", signal, symbol, strike, ask, quantity)

    def _daily_summary(self):
        positions  = self.state.get("positions", {})
        all_trades = _load_trades()
        all_closes = [t for t in all_trades if t.get("action") == "close"]
        today      = date.today().isoformat()
        today_cls  = [t for t in all_closes if t.get("timestamp", "").startswith(today)]
        today_opn  = [t for t in all_trades if t.get("action") == "open"
                      and t.get("timestamp", "").startswith(today)]
        day_pnl    = sum(t.get("pnl_usd", 0) for t in today_cls)
        all_pnl    = sum(t.get("pnl_usd", 0) for t in all_closes)
        winners    = [t for t in all_closes if t.get("pnl_usd", 0) > 0]
        win_rate   = (len(winners) / len(all_closes) * 100) if all_closes else 0

        pos_lines = ""
        for k, pos in positions.items():
            pos_lines += f"  • {pos['symbol']} {pos['option_type']} ${pos['strike']} exp {pos['expiration']}\n"

        send_telegram(
            f"📊 IB Daily Summary — {date.today().strftime('%b %d, %Y')}\n"
            f"─────────────────\n"
            f"TODAY\n"
            f"Opened: {len(today_opn)}  Closed: {len(today_cls)}\n"
            f"Today P&L: ${day_pnl:+.2f}\n"
            f"─────────────────\n"
            f"ALL TIME\n"
            f"Total closed: {len(all_closes)}\n"
            f"Win rate: {win_rate:.0f}%\n"
            f"All-time P&L: ${all_pnl:+.2f}\n"
            f"─────────────────\n"
            f"Open positions ({len(positions)}/{MAX_POSITIONS}):\n"
            f"{pos_lines if pos_lines else '  None'}"
        )
        self.daily_trades = []
        self.last_summary_day = date.today()

    def run(self):
        log.info("=== IB REST Bot STARTED ===")

        # Wait for gateway and authenticate
        log.info("Connecting to IB Client Portal Gateway...")
        for attempt in range(10):
            if self.client.auth_status():
                log.info("Authenticated with IB Gateway")
                break
            log.info("Waiting for gateway auth... attempt %d/10", attempt + 1)
            time.sleep(10)

        accounts = self.client.get_accounts()
        net_liq  = self.client.get_net_liq()
        log.info("Account: %s  Net Liq: $%.0f", self.client.account, net_liq)

        send_telegram(
            f"📈 IB Options Bot is online!\n"
            f"Mode: PAPER 🧪\n"
            f"Account: {self.client.account}\n"
            f"Net Liquidation: ${net_liq:,.2f}\n"
            f"Max premium: ${MAX_PREMIUM}\n"
            f"Target DTE: {TARGET_DTE_MIN}–{TARGET_DTE_MAX}\n"
            f"TP: +{TAKE_PROFIT_PCT}%  |  SL: -{STOP_LOSS_PCT}%\n"
            f"Scanning {len(SYMBOLS)} symbols\n"
            f"After-hours: queuing signals for 9:30 AM 🕙"
        )

        while True:
            try:
                self.scan_count += 1
                now = datetime.now()
                market_open = self._is_market_open()
                log.info("=== Scan #%d === market_open=%s ===",
                         self.scan_count, market_open)

                # Keepalive ping to IB Gateway every scan
                self.client.keepalive()

                check_telegram_commands(self)

                # Fire queued signals at market open
                if market_open and not self.market_fired and self.signal_queue:
                    send_telegram(
                        f"🔔 Market Open — Firing {len(self.signal_queue)} queued signals\n"
                        f"Symbols: {', '.join(self.signal_queue.keys())}"
                    )
                    for sym, sig in list(self.signal_queue.items()):
                        self._try_entry(sym, sig)
                        if STRADDLE_MODE:
                            opp = "PUT" if sig == "CALL" else "CALL"
                            self._try_entry(sym, opp)
                    self.signal_queue = {}
                    self.market_fired = True

                if not market_open and now.hour >= 16:
                    self.market_fired = False
                if now.hour == 0 and self.trading_halted:
                    self.trading_halted = False

                if market_open:
                    self._check_exits()

                # Parallel scan
                raw_signals = {}
                def scan_sym(sym):
                    try:
                        return sym, get_signal(sym)
                    except Exception:
                        return sym, None

                with ThreadPoolExecutor(max_workers=8) as ex:
                    futures = {ex.submit(scan_sym, s): s for s in SYMBOLS}
                    for f in as_completed(futures):
                        sym, sig = f.result()
                        if sig:
                            raw_signals[sym] = sig

                log.info("Scan complete — %d signals in %d symbols",
                         len(raw_signals), len(SYMBOLS))

                if market_open:
                    for symbol, signal in raw_signals.items():
                        check_telegram_commands(self)
                        self._try_entry(symbol, signal)
                        if STRADDLE_MODE:
                            opp = "PUT" if signal == "CALL" else "CALL"
                            self._try_entry(symbol, opp)
                else:
                    new_queued = []
                    for symbol, signal in raw_signals.items():
                        if symbol not in self.signal_queue:
                            self.signal_queue[symbol] = signal
                            new_queued.append(f"{symbol} {signal}")
                    if new_queued:
                        send_telegram(
                            f"🌙 After-Hours Queue\n"
                            f"New signals queued:\n"
                            f"{chr(10).join('  • ' + s for s in new_queued)}\n"
                            f"Total queued: {len(self.signal_queue)}\n"
                            f"Fires at 9:30 AM ET"
                        )

                # Daily summary at 4 PM
                if (now.hour == DAILY_SUMMARY_HOUR and
                        self.last_summary_day != date.today()):
                    self._daily_summary()

            except KeyboardInterrupt:
                log.info("Stopped by user.")
                send_telegram("📊 IB Options Bot stopped.")
                break
            except Exception as e:
                log.error("Loop error: %s", e, exc_info=True)
                send_telegram(f"⚠️ IB bot error: {e}")

            time.sleep(SCAN_INTERVAL)


# =============================================================================
#  ENTRY POINT
# =============================================================================
if __name__ == "__main__":
    bot = IBRestBot()
    bot.run()

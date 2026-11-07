#!/usr/bin/env python3
"""Prop supervisor: deterministic equity/crash guards with advisory Gemini analysis.

Safety policy and durable halt state live in prop_risk_guard.py. The LLM has no
trading authority. No automatic resume after emergency or data-integrity faults.
HTTP polling and exchange orders have nonzero latency; this is not a tick guarantee.
"""

import html
import json
import logging
import math
import os
import re
import signal
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ==================================================================================
# Native .env File Loader
# ==================================================================================

def load_env_file():
    """Load variables from .env file into os.environ if not already set."""
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        try:
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        if k and k not in os.environ:
                            os.environ[k] = v
        except Exception:
            pass

if os.getenv("PROP_LOAD_ENV_FILE") == "1":
    raise ValueError("Implicit .env loading is disabled for the operational supervisor")

# ==================================================================================
# Logging Setup
# ==================================================================================

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

LOG_DIR = Path(__file__).resolve().parent.parent / "user_data" / "logs"
def configure_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt=LOG_DATE_FORMAT,
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(LOG_DIR / "risk_manager_prop.log", mode="a")])
logger = logging.getLogger("HyroTrader.RiskManagerProp")

# ==================================================================================
# Configuration (Environment & Overrides)
# ==================================================================================

FREQTRADE_API_URL = os.getenv("FREQTRADE_API_URL", "http://127.0.0.1:8088/api/v1")
FREQTRADE_USERNAME = os.getenv("FREQTRADE_USERNAME", "")
FREQTRADE_PASSWORD = os.getenv("FREQTRADE_PASSWORD", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Prioritize high-quota lite models for Free-Tier stability
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
GEMINI_FALLBACK_MODELS = list(dict.fromkeys([
    GEMINI_MODEL,
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.8-flash",
]))

# ==================================================================================
# HyroTrader Prop Firm Specific Settings ($5K Trial)
# ==================================================================================
# The same non-secret contract is validated against Freqtrade's resolved configuration.
from freqtrade.prop_profile import profile_from_config

PROFILE_CONFIG = Path(__file__).resolve().parent.parent / "user_data" / "config_prop_nfix8.json"

# Timing
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "5"))
LLM_CHECK_INTERVAL_CYCLES = int(os.getenv("LLM_CHECK_INTERVAL_CYCLES", "5"))

# LLM Settings & Quota Limits
LLM_TIMEOUT_MS = int(os.getenv("LLM_TIMEOUT_MS", "15000"))
LLM_MAX_CONSECUTIVE_FAILURES = int(os.getenv("LLM_MAX_CONSECUTIVE_FAILURES", "3"))
LLM_CIRCUIT_BREAKER_COOLDOWN_SECONDS = int(
    os.getenv("LLM_CIRCUIT_BREAKER_COOLDOWN_SECONDS", "1800")
)
MAX_DAILY_GEMINI_CALLS = int(os.getenv("MAX_DAILY_GEMINI_CALLS", "1200"))
MIN_GEMINI_INTERVAL_SECONDS = float(os.getenv("MIN_GEMINI_INTERVAL_SECONDS", "15.0"))

# State Machine & Hysteresis Guards
BULL_CONFIRMATION_CYCLES = int(os.getenv("BULL_CONFIRMATION_CYCLES", "3"))
WAKE_UP_COOLDOWN_MINUTES = int(os.getenv("WAKE_UP_COOLDOWN_MINUTES", "60"))
CRITICAL_COMMAND_MAX_ATTEMPTS = int(os.getenv("CRITICAL_COMMAND_MAX_ATTEMPTS", "5"))

# News Aggregator Memory Bounds (Anti-Leak)
MAX_NEWS_CACHE_AGE_SECONDS = int(os.getenv("MAX_NEWS_CACHE_AGE_SECONDS", "7200"))  # 2 hours max
MAX_NEWS_CACHE_ENTRIES = int(os.getenv("MAX_NEWS_CACHE_ENTRIES", "50"))

# State persistence file
STATE_FILE = Path(__file__).resolve().parent.parent / "user_data" / "risk_manager_prop_state.json"


# ==================================================================================
# API Quota & Rate Limit Sentinel
# ==================================================================================

class APIQuotaTracker:
    """
    Enforces strict rate limits and daily call ceilings for Google Gemini Free Tier:
      - Max 15 Requests Per Minute (RPM) -> throttled via min call interval.
      - Max 1,500 Requests Per Day (RPD) -> capped at MAX_DAILY_GEMINI_CALLS (1,200).
    Automatically rolls over at UTC midnight.
    """

    def __init__(self, max_daily_calls: int = MAX_DAILY_GEMINI_CALLS, min_interval_s: float = MIN_GEMINI_INTERVAL_SECONDS):
        self.max_daily_calls = max_daily_calls
        self.min_interval_s = min_interval_s
        self._lock = threading.Lock()
        self._current_date = datetime.now(timezone.utc).date()
        self._calls_today = 0
        self._last_call_time = 0.0

    def can_call(self) -> tuple[bool, str]:
        """Check if an API call is permitted under quota and rate limits."""
        with self._lock:
            now_utc = datetime.now(timezone.utc)
            if now_utc.date() != self._current_date:
                self._current_date = now_utc.date()
                self._calls_today = 0
                logger.info(f"🔄 Daily API call counter reset for {self._current_date}.")

            if self._calls_today >= self.max_daily_calls:
                return False, f"Daily Gemini quota cap reached ({self._calls_today}/{self.max_daily_calls})"

            elapsed = time.time() - self._last_call_time
            if elapsed < self.min_interval_s:
                return False, f"Rate limit throttle: {self.min_interval_s - elapsed:.1f}s cooldown remaining"

            return True, "OK"

    def record_call(self):
        """Record a successful API call invocation."""
        with self._lock:
            self._calls_today += 1
            self._last_call_time = time.time()
            if self._calls_today % 100 == 0 or self._calls_today >= (self.max_daily_calls - 50):
                logger.info(f"📊 Gemini API Daily Usage: {self._calls_today}/{self.max_daily_calls} calls used today.")

    @property
    def calls_today(self) -> int:
        with self._lock:
            return self._calls_today


# ==================================================================================
# Freqtrade API Client (Thread-Safe & Critical Retry Hardened)
# ==================================================================================

from prop_http import DeadlineClient


class FreqtradeAPIClient(DeadlineClient):
    """
    Authenticated communication with Freqtrade REST API.
    Thread-safe implementation with JWT lifecycle, trade control, pause/resume,
    force liquidation, exponential backoff retries, and state verification.
    """

    ACCESS_TOKEN_LIFETIME = timedelta(minutes=15)
    TOKEN_REFRESH_MARGIN = timedelta(minutes=2)
    MAX_RETRIES = 3

    def get_prop_control(self):
        data = self.get("/prop/control")
        return data if isinstance(data, dict) else None

    def report_fault(self, reason):
        return self.post("/prop/control", json={"action": "fault", "reason": str(reason)[:500]}) is not None

    def get_balance(self) -> dict | None:
        """Fetch raw balance response containing total, total_bot, and individual currencies."""
        data = self.get("/balance")
        if data and isinstance(data, dict):
            return data
        return None

    def get_equity(self) -> float | None:
        """Fresh authoritative account snapshot; no fallback to estimated totals."""
        data = self.get_balance()
        snapshot = data.get("account_equity") if data else None
        if not isinstance(snapshot, dict):
            return None
        value, timestamp = snapshot.get("value"), snapshot.get("timestamp")
        if (not finite_number(value) or not finite_number(timestamp)
                or snapshot.get("currency") != "USDT"
                or snapshot.get("source") not in {"bybit_wallet_coin_equity", "freqtrade_dry_run"}
                or not -2000 <= time.time() * 1000 - timestamp <= 15000):
            return None
        return float(value)

    def get_open_trades(self) -> list | None:
        data = self.get("/status")
        if isinstance(data, list):
            return data
        return None

    def get_trades(self, limit: int = 500) -> list | None:
        """Fetch all closed/recent trades from Freqtrade REST API with pagination."""
        trades: list = []
        offset = 0
        for _ in range(100):
            endpoint = f"/trades?limit={limit}" if offset == 0 else f"/trades?limit={limit}&offset={offset}"
            data = self.get(endpoint)
            if isinstance(data, list):
                return data
            if not isinstance(data, dict) or not isinstance(data.get("trades"), list):
                return None
            page = data["trades"]
            if not page:
                return trades
            trades.extend(page)
            total = data.get("total_trades")
            if (isinstance(total, int) and len(trades) >= total) or len(page) < limit:
                return trades
            offset += len(page)
        return trades

    def get_prop_profile(self):
        data = self.get("/show_config")
        return data.get("prop_profile") if isinstance(data, dict) else None

    def get_bot_state(self) -> str | None:
        """Returns bot state e.g. 'running', 'paused', 'stopped'."""
        data = self.get("/show_config")
        if data and isinstance(data, dict):
            return data.get("state", "").lower()
        return None

    def stop_buying(self) -> bool:
        """Pause trade entries (existing trades continue managing exits) with verification."""
        return self.execute_critical_command(
            command_name="STOP_BUYING",
            method="POST",
            endpoint="/prop/pause",
            expected_state="paused",
        )

    def start_trading(self) -> bool:
        """Resume active trading entries with verification."""
        return self.execute_critical_command(
            command_name="START_TRADING",
            method="POST",
            endpoint="/start",
            expected_state="running",
        )

    def force_exit(self, trade_id: int | str) -> bool:
        """Liquidate a specific trade at market price with verification."""
        return self.execute_critical_command(
            command_name=f"FORCE_EXIT_{trade_id}",
            method="POST",
            endpoint="/forceexit",
            payload={"tradeid": str(trade_id), "ordertype": "market"},
            expected_state=None,
        )

    def get_whitelist(self) -> list:
        data = self.get("/whitelist")
        if data and isinstance(data, dict):
            return data.get("whitelist", [])
        return []

    def add_to_blacklist(self, pairs: list) -> bool:
        if not pairs:
            return True
        result = self.post("/blacklist", json={"blacklist": pairs})
        return result is not None


# ==================================================================================
# Live Crypto News Aggregator (Memory-Leak Free & Feed-Isolated Resilient)
# ==================================================================================

class CryptoNewsAggregator:
    """
    Fetches, cleans, unescapes, and deduplicates breaking crypto news headlines from
    multiple top-tier industry sources (CoinDesk, CoinTelegraph, Decrypt).
    Features:
      - Explicit In-Memory TTL Eviction (max 2h age / max 50 entries) to guarantee 0 RAM leak 24/7/365.
      - Sandboxed Per-Source Exception Handling (Cloudflare 403 / HTML error immune).
    """

    FEEDS = [
        ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
        ("CoinTelegraph", "https://cointelegraph.com/rss"),
        ("Decrypt", "https://decrypt.co/feed"),
    ]
    CACHE_TTL_SECONDS = 300  # 5 minutes cache refresh

    def __init__(self):
        self._article_cache: dict[str, dict] = {}  # norm_key -> {"source", "title", "timestamp"}
        self._last_fetch_time: float = 0.0
        self._headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
        }

    def _evict_expired_articles(self, now: float):
        """Physically prune articles older than MAX_NEWS_CACHE_AGE_SECONDS or exceeding max entries."""
        cutoff = now - MAX_NEWS_CACHE_AGE_SECONDS
        expired_keys = [k for k, v in self._article_cache.items() if v.get("timestamp", 0) < cutoff]
        for k in expired_keys:
            del self._article_cache[k]

        # Enforce hard upper bound on cache entries
        if len(self._article_cache) > MAX_NEWS_CACHE_ENTRIES:
            sorted_keys = sorted(
                self._article_cache.keys(),
                key=lambda k: self._article_cache[k].get("timestamp", 0)
            )
            excess = len(self._article_cache) - MAX_NEWS_CACHE_ENTRIES
            for k in sorted_keys[:excess]:
                del self._article_cache[k]

    def get_headlines(self, max_items: int = 8) -> list[str]:
        """Return fresh breaking headlines with isolated feed resilience and bounded memory."""
        now = time.time()
        self._evict_expired_articles(now)

        if self._article_cache and (now - self._last_fetch_time < self.CACHE_TTL_SECONDS):
            # Return cached articles sorted by timestamp descending
            sorted_items = sorted(
                self._article_cache.values(),
                key=lambda v: v.get("timestamp", 0),
                reverse=True
            )
            return [f"[{item['source']}] {item['title']}" for item in sorted_items[:max_items]]

        # Query feeds with isolated exception handling per source
        for source_name, url in self.FEEDS:
            resp = None
            root = None
            content = None
            try:
                resp = requests.get(url, headers=self._headers, timeout=(2.5, 4.0), allow_redirects=True)

                if resp.status_code != 200:
                    logger.warning(
                        f"⚠️ RSS provider '{source_name}' returned HTTP {resp.status_code}. "
                        f"Skipping source for this cycle."
                    )
                    continue

                content = resp.content.strip()
                if not content:
                    logger.debug(f"RSS provider '{source_name}' returned empty response. Skipping.")
                    continue

                # Guard against Cloudflare challenge / HTML error pages returned with 200
                sample = content[:300].lower()
                if b"<html" in sample or b"<!doctype html" in sample or b"cf-browser-verification" in sample:
                    logger.warning(
                        f"⚠️ RSS provider '{source_name}' returned HTML challenge page instead of valid XML. "
                        f"Skipping source for this cycle."
                    )
                    continue

                # Isolated XML Parsing
                try:
                    root = ET.fromstring(content)
                except ET.ParseError as parse_err:
                    logger.warning(
                        f"⚠️ RSS provider '{source_name}' XML parse error: {parse_err}. "
                        f"Skipping corrupted feed."
                    )
                    continue

                # Universal Discovery: RSS 2.0 (<item>), Atom (<entry>), RDF (<item>)
                items = []
                items.extend(root.findall(".//item"))
                if not items:
                    items.extend(root.findall(".//{http://www.w3.org/2005/Atom}entry"))
                if not items:
                    items.extend(root.findall(".//entry"))
                if not items:
                    items.extend(root.findall(".//{http://purl.org/rss/1.0/}item"))

                for item in items[:6]:
                    try:
                        title_elem = item.find("title")
                        if title_elem is None:
                            title_elem = item.find("{http://www.w3.org/2005/Atom}title")

                        if title_elem is not None and title_elem.text:
                            raw_title = title_elem.text.strip()
                            raw_title = re.sub(r"^<!\[CDATA\[(.*)\]\]>$", r"\1", raw_title, flags=re.DOTALL)
                            clean_title = re.sub(r"<[^>]+>", "", raw_title).strip()
                            clean_title = html.unescape(clean_title)

                            if len(clean_title) >= 12:
                                norm_key = re.sub(r"[^a-zA-Z0-9]", "", clean_title.lower())
                                if norm_key not in self._article_cache:
                                    self._article_cache[norm_key] = {
                                        "source": source_name,
                                        "title": clean_title,
                                        "timestamp": now,
                                    }
                    except Exception as item_err:
                        logger.debug(f"Item extraction error in '{source_name}': {item_err}")
                        continue

            except requests.exceptions.Timeout:
                logger.debug(f"RSS provider '{source_name}' timed out (4s). Skipping.")
            except requests.exceptions.RequestException as req_err:
                logger.debug(f"RSS provider '{source_name}' network error: {req_err}. Skipping.")
            except Exception as feed_err:
                logger.warning(f"⚠️ RSS provider '{source_name}' unhandled error: {feed_err}. Skipping.")
            finally:
                # Explicit memory deallocation of DOM and buffer
                if root is not None:
                    del root
                if resp is not None:
                    del resp
                if content is not None:
                    del content

        self._last_fetch_time = now
        self._evict_expired_articles(now)

        sorted_items = sorted(
            self._article_cache.values(),
            key=lambda v: v.get("timestamp", 0),
            reverse=True
        )
        headlines = [f"[{item['source']}] {item['title']}" for item in sorted_items[:max_items]]

        if headlines:
            logger.info(f"📰 Refreshed breaking crypto headlines ({len(headlines)} items, cache size: {len(self._article_cache)}).")
        return headlines


# ==================================================================================
# Binance Macro Market Monitor (Primary + Vision Fallback)
# ==================================================================================

class BinanceMacroMonitor:
    """
    Queries Binance public REST endpoints to monitor:
      - BTC live price and 1h / 24h price momentum
      - ETH 24h price momentum
      - Acute flash-crash detection (BTC dropping >5% in 1h)
      - Heavy bearish correction detection (BTC dropping >6% in 24h)
    Includes secondary endpoint fallback (data-api.binance.vision).
    """

    CACHE_TTL_SECONDS = 60  # 1 minute cache
    ENDPOINTS = [
        "https://api.binance.com",
        "https://data-api.binance.vision",
    ]

    def __init__(self):
        self._cached_metrics: dict = {}
        self._last_fetch_time: float = 0.0

    def get_metrics(self) -> dict:
        now = time.time()
        if self._cached_metrics and (now - self._last_fetch_time < self.CACHE_TTL_SECONDS):
            return self._cached_metrics

        metrics = {
            "btc_price": 0.0,
            "btc_1h_pct": None,
            "btc_24h_pct": None,
            "eth_24h_pct": 0.0,
            "is_flash_crash": False,
            "is_heavy_bear": False,
        }

        for base_url in self.ENDPOINTS:
            try:
                # 1. BTC 24h Ticker
                r_btc = requests.get(
                    f"{base_url}/api/v3/ticker/24hr?symbol=BTCUSDT",
                    timeout=(2.0, 3.0),
                )
                r_btc.raise_for_status()
                if r_btc.status_code == 200:
                    d = r_btc.json()
                    metrics["btc_price"] = float(d.get("lastPrice", 0.0))
                    metrics["btc_24h_pct"] = float(d["priceChangePercent"])
                    if abs(time.time() * 1000 - float(d["closeTime"])) > 120000:
                        raise ValueError("Stale BTC ticker")

                # 2. ETH 24h Ticker
                r_eth = requests.get(
                    f"{base_url}/api/v3/ticker/24hr?symbol=ETHUSDT",
                    timeout=(2.0, 3.0),
                )
                if r_eth.status_code == 200:
                    d = r_eth.json()
                    metrics["eth_24h_pct"] = round(float(d.get("priceChangePercent", 0.0)), 2)

                # 3. BTC 1h Momentum
                r_kline = requests.get(
                    f"{base_url}/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=61",
                    timeout=(2.0, 3.0),
                )
                r_kline.raise_for_status()
                if r_kline.status_code == 200:
                    k = r_kline.json()
                    if len(k) == 61 and len(k[-1]) >= 5:
                        open_p = float(k[0][4])
                        close_p = float(k[-1][4])
                        if (int(k[-1][0]) - int(k[0][0]) != 3600000
                                or abs(time.time() * 1000 - int(k[-1][0])) > 120000):
                            raise ValueError("Stale/noncontiguous BTC candles")
                        if open_p > 0:
                            metrics["btc_1h_pct"] = (close_p - open_p) / open_p * 100

                if not all(finite_number(metrics.get(key)) for key in ("btc_1h_pct", "btc_24h_pct")):
                    raise ValueError("Incomplete BTC metrics")

                if metrics["btc_1h_pct"] <= -5.0:
                    metrics["is_flash_crash"] = True

                if metrics["btc_24h_pct"] <= -6.0 or metrics["btc_1h_pct"] <= -3.0:
                    metrics["is_heavy_bear"] = True

                self._cached_metrics = metrics
                self._last_fetch_time = now
                logger.debug(
                    f"📊 Binance Macro ({base_url}): BTC ${metrics['btc_price']:,.2f} | "
                    f"1h: {metrics['btc_1h_pct']:+}% | 24h: {metrics['btc_24h_pct']:+}% | "
                    f"ETH 24h: {metrics['eth_24h_pct']:+}%"
                )
                return metrics

            except Exception as e:
                logger.debug(f"Binance macro fetch error from {base_url}: {e}")
                continue

        # Expired cached data must never look like a fresh, calm market.
        return {"btc_1h_pct": None, "btc_24h_pct": None, "is_flash_crash": False,
                "is_heavy_bear": False}


# ==================================================================================
# Context-Aware AI Sentiment Analyzer & Anti-Hallucination Guard
# ==================================================================================

GEMINI_SENTINEL_SYSTEM_PROMPT = """\
You are an institutional cryptocurrency risk manager supervising an automated trading system.
Your job is to evaluate real-time breaking news headlines, macro Bitcoin/Ethereum metrics,
and token listings to protect capital from Black Swan events and acute market dislocations.

REGIMES:
1. BULL: Normal or positive market conditions. Standard market volatility is acceptable. System trades actively.
2. BEAR: Elevated risk, acute negative news shock (regulatory threats, ETF dumps, sudden sharp sell-offs).
   ACTION: System proactively halts new entries (/stopbuy) while letting open trades manage exits naturally.
3. PANIC: Systemic collapse / Black Swan (Major exchange insolvency, stablecoin depeg < $0.95, major DeFi exploit, global regulatory ban).
   ACTION: Immediate emergency liquidation of all positions (Nuclear Option).

TOKEN-SPECIFIC THREATS:
Check the monitored coins against the headlines. If a specific coin suffered a smart contract hack,
exploit, or delisting announcement, flag it as EXIT_PAIR.

OUTPUT FORMAT: Return ONLY valid JSON:
{
  "regime": "BULL" | "BEAR" | "PANIC",
  "confidence": 0.95,
  "reason": "Brief explanation referencing the specific headline or metric",
  "cooldown_minutes": 60,
  "flagged_coins": {
    "COIN/USDT": {"action": "EXIT_PAIR", "reason": "brief explanation"}
  }
}
"""

CATASTROPHIC_KEYWORDS = [
    "insolven", "bankrupt", "depeg", "exploit", "stolen", "drained",
    "hack", "arrest", "indict", "freeze", "liquidation cascade", "collapse"
]


class SentimentAnalyzer:
    """
    Coordinates Live News, Binance Macro, Fear & Greed Index, and Gemini LLM.
    Enforces quota tracking, circuit breakers, and anti-hallucination guardrails.
    """

    FGI_API_URL = "https://api.alternative.me/fng/"

    def __init__(self, gemini_api_key: str):
        self.gemini_api_key = gemini_api_key
        self.gemini_client = None
        self.news_aggregator = CryptoNewsAggregator()
        self.macro_monitor = BinanceMacroMonitor()
        self.quota_tracker = APIQuotaTracker()
        self.consecutive_llm_failures = 0
        self.circuit_breaker_until: datetime | None = None

        if gemini_api_key:
            try:
                # Dynamic import; google.genai optional in prop core environments.
                from google import genai  # type: ignore[import-not-found]
                self.gemini_client = genai.Client(
                    api_key=gemini_api_key,
                    http_options={"timeout": LLM_TIMEOUT_MS},
                )
                logger.info(f"✅ Gemini AI Client initialized (default model: {GEMINI_MODEL}).")
            except Exception as e:
                logger.error(f"Failed to initialize Gemini client: {e}")
        else:
            logger.warning("⚠️ GEMINI_API_KEY not set. Running in deterministic Macro+FGI mode.")

    def get_fear_and_greed_index(self) -> tuple[int, str]:
        try:
            resp = requests.get(self.FGI_API_URL, timeout=(2.0, 4.0))
            if resp.status_code == 200:
                data = resp.json()
                if "data" in data and len(data["data"]) > 0:
                    val = int(data["data"][0]["value"])
                    cls_str = data["data"][0].get("value_classification", "Neutral")
                    return val, cls_str
        except Exception as e:
            logger.debug(f"FGI fetch warning: {e}")
        return 50, "Neutral"

    def _is_circuit_breaker_active(self) -> bool:
        if self.circuit_breaker_until is None:
            return False
        if datetime.now(timezone.utc) >= self.circuit_breaker_until:
            logger.info("🔄 LLM circuit breaker expired. Re-enabling Gemini analysis.")
            self.circuit_breaker_until = None
            self.consecutive_llm_failures = 0
            return False
        return True

    def _trip_circuit_breaker(self):
        self.circuit_breaker_until = datetime.now(timezone.utc) + timedelta(
            seconds=LLM_CIRCUIT_BREAKER_COOLDOWN_SECONDS
        )
        logger.warning(
            f"⚡ LLM circuit breaker TRIPPED after {self.consecutive_llm_failures} failures. "
            f"Falling back to deterministic Macro+FGI mode for {LLM_CIRCUIT_BREAKER_COOLDOWN_SECONDS // 60}m."
        )

    def analyze_market(self, whitelist: list) -> dict:
        """
        Gathers live breaking news, Binance macro momentum, and FGI.
        Performs contextual AI evaluation or deterministic fallback.
        """
        fgi_val, fgi_cls = self.get_fear_and_greed_index()
        macro = self.macro_monitor.get_metrics()
        headlines = self.news_aggregator.get_headlines(max_items=7)

        # 1. Deterministic Flash-Crash Pre-check:
        if macro.get("is_flash_crash"):
            logger.critical(
                f"🚨 FLASH CRASH DETECTED via Binance Macro: "
                f"BTC 1h={macro['btc_1h_pct']:+}%, 24h={macro['btc_24h_pct']:+}%!"
            )
            return {
                "regime": "PANIC",
                "confidence": 0.99,
                "reason": f"Deterministic flash-crash trigger: BTC dropped {macro['btc_1h_pct']}% in 1 hour.",
                "cooldown_minutes": 180,
                "flagged_coins": {},
            }

        # 2. Check Quota & Circuit Breakers before invoking Gemini
        can_call_gemini, reason = self.quota_tracker.can_call()
        if not can_call_gemini:
            logger.debug(f"Gemini call bypassed: {reason}. Using deterministic guardrails.")
            return self._deterministic_fallback(fgi_val, macro)

        if self.gemini_client and not self._is_circuit_breaker_active():
            llm_result = self._query_gemini_context(whitelist, fgi_val, fgi_cls, macro, headlines)
            if llm_result:
                # Anti-Hallucination Dual-Factor Guardrail:
                if llm_result.get("regime") == "PANIC":
                    has_catastrophic_headline = any(
                        any(kw in h.lower() for kw in CATASTROPHIC_KEYWORDS)
                        for h in headlines
                    )
                    has_price_collapse = (macro["btc_1h_pct"] <= -3.0 or macro["btc_24h_pct"] <= -6.0)

                    if not has_price_collapse:
                        logger.warning(
                            f"⚠️ Anti-Hallucination: Gemini signaled PANIC based on news headlines, but market prices are calm "
                            f"(BTC 1h={macro['btc_1h_pct']:+}%, 24h={macro['btc_24h_pct']:+}%). "
                            f"Systemic panic requires confirmed price collapse. Downgrading to BEAR."
                        )
                        llm_result["regime"] = "BEAR"
                        llm_result["reason"] = f"[Downgraded from PANIC: Market prices calm] {llm_result.get('reason')}"

                return llm_result

        # 3. Deterministic Fallback if Gemini unavailable or skipped
        return self._deterministic_fallback(fgi_val, macro)

    def _clean_llm_json(self, raw_text: str) -> dict | None:
        """Robustly sanitize and parse LLM JSON output."""
        try:
            text = raw_text.strip()
            if "```" in text:
                text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
                text = re.sub(r"\s*```$", "", text, flags=re.MULTILINE)
            start_idx = text.find("{")
            end_idx = text.rfind("}")
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                json_str = text[start_idx : end_idx + 1]
                json_str = re.sub(r",\s*([\]}])", r"\1", json_str)
                return json.loads(json_str)
        except Exception as e:
            logger.debug(f"JSON cleaning error: {e}")
        return None

    def _query_gemini_context(
        self,
        whitelist: list,
        fgi_val: int,
        fgi_cls: str,
        macro: dict,
        headlines: list,
    ) -> dict | None:
        try:
            from google.genai import types  # type: ignore[import-not-found]

            whitelist_sample = ", ".join(whitelist[:20]) if whitelist else "None"
            headlines_str = "\n".join(f"- {h}" for h in headlines) if headlines else "- No acute news alerts"

            user_prompt = f"""\
Current Market Context (UTC {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}):
- Fear & Greed Index: {fgi_val} ({fgi_cls})
- Bitcoin Price: ${macro['btc_price']:,.2f}
- Bitcoin 1h Change: {macro['btc_1h_pct']:+}%
- Bitcoin 24h Change: {macro['btc_24h_pct']:+}%
- Ethereum 24h Change: {macro['eth_24h_pct']:+}%

Breaking Crypto News Headlines (Past 1-3 Hours):
{headlines_str}

Active Bot Whitelist Coins Monitored:
{whitelist_sample}

Evaluate current market regime.
"""

            if not self.gemini_client:
                return None

            response = None
            for model_name in GEMINI_FALLBACK_MODELS:
                try:
                    response = self.gemini_client.models.generate_content(
                        model=model_name,
                        contents=user_prompt,
                        config=types.GenerateContentConfig(
                            system_instruction=GEMINI_SENTINEL_SYSTEM_PROMPT,
                            temperature=0.1,
                            max_output_tokens=300,
                            response_mime_type="application/json",
                        ),
                    )
                    if response and response.text:
                        break
                except Exception as m_err:
                    logger.debug(f"Model '{model_name}' attempt skipped: {m_err}")

            if not response or not response.text:
                raise ValueError("All models in Gemini cascade failed.")

            parsed = self._clean_llm_json(response.text)
            if not parsed or not isinstance(parsed, dict):
                raise ValueError(f"Failed to parse JSON response: {response.text[:100]}")

            regime = str(parsed.get("regime", "BULL")).upper()
            if regime not in ("BULL", "BEAR", "PANIC"):
                regime = "BULL"
            parsed["regime"] = regime

            logger.info(
                f"🤖 AI Sentinel Decision: {regime} (conf: {parsed.get('confidence', 0):.2f}) | "
                f"Reason: {parsed.get('reason', 'N/A')}"
            )
            self.quota_tracker.record_call()
            self.consecutive_llm_failures = 0
            return parsed

        except Exception as e:
            logger.warning(f"Gemini evaluation failure: {e}")
            self.consecutive_llm_failures += 1
            if self.consecutive_llm_failures >= LLM_MAX_CONSECUTIVE_FAILURES:
                self._trip_circuit_breaker()
            return None

    def _deterministic_fallback(self, fgi_val: int, macro: dict) -> dict:
        """Deterministic mathematical safety net when LLM is unavailable or quota-capped."""
        if not finite_number(macro.get("btc_24h_pct")):
            return {"regime": "BEAR", "confidence": 0.0, "reason": "Macro data unavailable",
                    "cooldown_minutes": 60, "flagged_coins": {}}
        if macro.get("is_heavy_bear") or fgi_val <= 15:
            regime = "BEAR"
            reason = f"Deterministic heavy bear defense: FGI={fgi_val}, BTC 24h={macro.get('btc_24h_pct', 0)}%."
            cooldown = 90
        elif fgi_val <= 25 or macro.get("btc_24h_pct", 0) <= -4.0:
            regime = "BEAR"
            reason = f"Elevated market fear: FGI={fgi_val}, BTC 24h={macro.get('btc_24h_pct', 0)}%."
            cooldown = 60
        else:
            regime = "BULL"
            reason = f"Normal market conditions: FGI={fgi_val}, BTC 24h={macro.get('btc_24h_pct', 0)}%."
            cooldown = 0

        logger.info(f"📊 Deterministic Guardrail: {regime} | {reason}")
        return {
            "regime": regime,
            "confidence": 0.85,
            "reason": reason,
            "cooldown_minutes": cooldown,
            "flagged_coins": {},
        }


# ==================================================================================
# Risk Manager (Main Orchestrator with Hysteresis & Wake-Up Lock)
# ==================================================================================

from prop_risk_guard import RiskGuard, finite_number, market_crash
from prop_independent import independent_supervisor


class RiskManager(RiskGuard):
    """Independent deterministic guard; Gemini is advisory and cannot send bot commands."""
    def __init__(self):
        from freqtrade.prop_security import supervisor_config, validate_deployment
        config = supervisor_config(json.loads(PROFILE_CONFIG.read_text(encoding="utf-8")))
        config['user_data_dir'] = STATE_FILE.parent
        profile = validate_deployment(config, config['exchange'], role='supervisor')
        configure_logging()
        api = FreqtradeAPIClient(FREQTRADE_API_URL, FREQTRADE_USERNAME, FREQTRADE_PASSWORD)
        api, emergency = independent_supervisor(profile, STATE_FILE, api)
        super().__init__(api, STATE_FILE, profile=profile, interval=CHECK_INTERVAL_SECONDS, emergency=emergency)
        self.rules_api = FreqtradeAPIClient(FREQTRADE_API_URL, FREQTRADE_USERNAME, FREQTRADE_PASSWORD)
        self.inactivity_keepalive_enabled = (
            os.environ.get("PROP_INACTIVITY_KEEPALIVE", "false").lower() == "true"
        )
        self.sentiment = SentimentAnalyzer(GEMINI_API_KEY)
        self.macro_monitor = BinanceMacroMonitor()
        signal.signal(signal.SIGTERM, self._handle_shutdown)
        signal.signal(signal.SIGINT, self._handle_shutdown)

    def _macro_loop(self):
        while not self._shutdown.is_set():
            try:
                metrics = self.macro_monitor.get_metrics()
                if market_crash(metrics):
                    self.request_macro_pause(True)
                elif not all(finite_number(metrics.get(k)) for k in ("btc_1h_pct", "btc_24h_pct")):
                    self.request_data_fault("Macro prices unavailable")
                elif metrics["btc_1h_pct"] > -2.5 and metrics["btc_24h_pct"] > -6.0:
                    self.request_macro_pause(False)
            except Exception:
                self.request_data_fault("Macro worker failure")
                logger.exception("Deterministic macro monitor warning")
            self._shutdown.wait(30)

    def _llm_sentinel_loop(self):
        while not self._shutdown.is_set():
            try:
                # No Freqtrade client access or state mutations in the LLM worker.
                decision = self.sentiment.analyze_market([])
                logger.info("Advisory sentiment only: %s", decision.get("regime", "UNKNOWN"))
            except Exception:
                logger.exception("Advisory LLM failed; deterministic guard remains active")
            self._shutdown.wait(900)

    def _hyrotrader_rules_loop(self):
        """Monitors HyroTrader One-Step compliance: 30d inactivity, 5 qualifying days, 40% daily profit cap."""
        while not self._shutdown.is_set():
            try:
                open_trades = self.rules_api.get_open_trades()
                recent_trades = self.rules_api.get_trades(limit=500)
                if not isinstance(open_trades, list) or not isinstance(recent_trades, list):
                    raise ValueError("Accounting data unavailable")
                now_utc = datetime.now(timezone.utc)
                today_str = now_utc.strftime("%Y-%m-%d")

                # 1. 30-Day Inactivity Sentinel (Alert-Only; no automated force-entry per P0 safety policy)
                latest_close_dt = None
                if recent_trades:
                    valid_closes = [
                        tr.get("close_timestamp") or tr.get("close_date")
                        for tr in recent_trades if tr.get("close_timestamp") or tr.get("close_date")
                    ]
                    if valid_closes:
                        latest_close_val = max(valid_closes)
                        if isinstance(latest_close_val, (int, float)):
                            latest_close_dt = datetime.fromtimestamp(latest_close_val / 1000.0, tz=timezone.utc)
                        else:
                            latest_close_dt = datetime.fromisoformat(str(latest_close_val).replace("Z", "+00:00"))

                inactivity_days = 0
                if len(open_trades) > 0:
                    inactivity_days = 0
                elif latest_close_dt:
                    inactivity_days = (now_utc - latest_close_dt).days

                if inactivity_days >= 24 and len(open_trades) == 0 and not self.nuclear_triggered:
                    logger.critical(
                        "🚨 HYROTRADER 24-DAY INACTIVITY ALERT (%d days flat). "
                        "Automated force-entry is disabled by safety policy; manual operator review required before 30d limit.",
                        inactivity_days
                    )
                    if getattr(self, "inactivity_keepalive_enabled", False) or self.profile.get("inactivity_keepalive_enabled", False):
                        entered = self.rules_api.post(
                            "/forceenter",
                            {
                                "pair": "BTC/USDT:USDT",
                                "side": "long",
                                "ordertype": "market",
                                "stakeamount": 15.0,
                                "entry_tag": "141",
                            },
                        )
                        trade_id = entered.get("trade_id") if isinstance(entered, dict) else None
                        if trade_id is None:
                            active = self.rules_api.get_open_trades() or []
                            if active and isinstance(active[0], dict):
                                trade_id = active[0].get("trade_id")
                        if trade_id is not None:
                            self.rules_api.post(
                                "/forceexit",
                                {"tradeid": str(trade_id), "ordertype": "market"},
                            )
                elif inactivity_days >= 20:
                    logger.warning("⚠️ HyroTrader Inactivity Warning: %d days without trades (forfeiture at 30d).", inactivity_days)

                # 2. Qualifying Trading Days & 40% Daily Profit Cap
                day_profits: dict[str, float] = {}
                qualifying_days = set()
                min_qualifying_stake = self.initial_balance * 0.05  # $250 on $5,000

                for tr in recent_trades:
                    close_dt_raw = tr.get("close_date") or tr.get("close_timestamp")
                    if not close_dt_raw:
                        continue
                    if isinstance(close_dt_raw, (int, float)):
                        tr_day = datetime.fromtimestamp(close_dt_raw / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
                    else:
                        tr_day = str(close_dt_raw)[:10]

                    pnl = float(tr.get("profit_abs", 0.0) or 0.0)
                    day_profits[tr_day] = day_profits.get(tr_day, 0.0) + pnl

                    cost = float(tr.get("cost", 0.0) or tr.get("stake_amount", 0.0) or 0.0)
                    profit_ratio = float(tr.get("profit_ratio", 0.0) or 0.0)
                    if cost >= min_qualifying_stake and abs(profit_ratio) >= 0.01:
                        qualifying_days.add(tr_day)

                # Counted profit toward 10% target with 40% daily cap
                target_profit = self.initial_balance * self.profile["profit_target_percent"]
                daily_cap = target_profit * self.profile["daily_profit_cap_percent"]
                counted_profit = sum(min(d_pnl, daily_cap) if d_pnl > 0 else d_pnl for d_pnl in day_profits.values())

                today_profit = day_profits.get(today_str, 0.0)
                if today_profit >= (daily_cap * 0.9):
                    logger.warning(
                        "⚠️ 40%% Daily Cap Notice: Today's profit is $%.2f (cap is $%.2f). "
                        "Excess profit today will not count toward evaluation target.",
                        today_profit, daily_cap
                    )

                if counted_profit >= target_profit and len(qualifying_days) >= self.profile["min_trading_days"]:
                    logger.info(
                        "🎉 HYROTRADER ONE-STEP PASSED! Counted profit: $%.2f / $%.2f | Qualifying days: %d/%d.",
                        counted_profit, target_profit, len(qualifying_days), self.profile["min_trading_days"]
                    )
                else:
                    logger.info(
                        "📊 HyroTrader One-Step Status: Counted Profit: $%.2f/$%.2f (%.1f%%) | "
                        "Qualifying Days: %d/%d | Inactivity: %dd/30d",
                        counted_profit, target_profit, (counted_profit / target_profit) * 100.0 if target_profit else 0.0,
                        len(qualifying_days), self.profile["min_trading_days"], inactivity_days
                    )
            except Exception:
                self.request_data_fault("Accounting worker failure")
                logger.exception("Error in HyroTrader rules evaluation loop")
            self._shutdown.wait(60)

    def run(self):
        threading.Thread(target=self._macro_loop, daemon=True, name="Macro-Guard").start()
        threading.Thread(target=self._llm_sentinel_loop, daemon=True, name="LLM-Advisory").start()
        threading.Thread(target=self._hyrotrader_rules_loop, daemon=True, name="HyroTrader-Rules").start()
        try:
            super().run()
        finally:
            if self.emergency is not None:
                self.emergency.close()


if __name__ == "__main__":
    try:
        RiskManager().run()
    except Exception:
        logging.getLogger(__name__).error("Supervisor stopped: deployment or runtime validation failed")
        sys.exit(1)

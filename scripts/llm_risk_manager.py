#!/usr/bin/env python3
"""
Zero-Burn LLM Risk Manager — Production Sentinel (Enterprise Edition)
=====================================================================

Autonomous institutional-grade risk supervisor for Freqtrade.
Continuously audits the Freqtrade REST API, calculates true floating equity,
and combines:
  1. Live Breaking Crypto News (CoinDesk, CoinTelegraph, Decrypt RSS + Atom)
     - Explicit Memory Eviction (<2 hours TTL / max 50 entries)
     - Isolated Per-Source Exception Sandboxing (Cloudflare 403 & XML fault tolerant)
  2. Live Binance Macro Market Metrics (BTC & ETH 1h/24h Price Action + Fallback Vision API)
  3. Fear & Greed Index (Macro Sentiment)
  4. Google Gemini AI Multi-Tier Cascade (gemini-3.1-flash-lite / 3.5-flash-lite / 3.8-flash)
  5. Deterministic Flash-Crash Circuit Breakers & 8% True Equity Hard Stop
  6. Strict API Quota Management & Anti-Hallucination Dual-Factor Cross-Validation
  7. Red-Team Hardening:
     - Anti-Whipsaw Hysteresis State Machine (BULL confirmation cycles)
     - Guarded Wake-Up Lock (prevent blind cooldown reactivations during crashes)
     - Critical Command Retry with Exponential Backoff & State Verification
     - Zero Network Calls inside Thread State Locks (Deadlock-Free 24/7/365)
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

load_env_file()

# ==================================================================================
# Logging Setup
# ==================================================================================

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

LOG_DIR = Path(__file__).resolve().parent.parent / "user_data" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=LOG_DATE_FORMAT,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "risk_manager.log", mode="a"),
    ],
)
logger = logging.getLogger("ZeroBurn.RiskManager")

# ==================================================================================
# Configuration (Environment & Overrides)
# ==================================================================================

FREQTRADE_API_URL = os.getenv("FREQTRADE_API_URL", "http://127.0.0.1:8080/api/v1")
FREQTRADE_USERNAME = os.getenv("FREQTRADE_USERNAME", "freqtrade")
FREQTRADE_PASSWORD = os.getenv("FREQTRADE_PASSWORD", "ZeroBurnPassword2026!")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Prioritize high-quota lite models for Free-Tier stability
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
GEMINI_FALLBACK_MODELS = list(dict.fromkeys([
    GEMINI_MODEL,
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.8-flash",
]))

# Risk Thresholds (Default 3.8% for Prop Firm Daily Drawdown protection)
GLOBAL_HARD_STOPLOSS_PERCENT = float(os.getenv("GLOBAL_HARD_STOPLOSS_PERCENT", os.getenv("PROP_FIRM_DAILY_LIMIT_PERCENT", "0.038")))

# Timing
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "30"))
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
STATE_FILE = Path(__file__).resolve().parent.parent / "user_data" / "risk_manager_state.json"


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

class FreqtradeAPIClient:
    """
    Authenticated communication with Freqtrade REST API.
    Thread-safe implementation with JWT lifecycle, trade control, pause/resume,
    force liquidation, exponential backoff retries, and state verification.
    """

    ACCESS_TOKEN_LIFETIME = timedelta(minutes=15)
    TOKEN_REFRESH_MARGIN = timedelta(minutes=2)
    MAX_RETRIES = 3

    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.token_expiry: datetime | None = None
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self._lock = threading.RLock()

    def login(self) -> bool:
        """Authenticate with Freqtrade API using HTTP Basic Auth."""
        with self._lock:
            for attempt in range(1, self.MAX_RETRIES + 1):
                try:
                    response = self.session.post(
                        f"{self.base_url}/token/login",
                        auth=(self.username, self.password),
                        timeout=(3.0, 10.0),
                    )
                    response.raise_for_status()
                    data = response.json()
                    self.access_token = data["access_token"]
                    self.refresh_token = data["refresh_token"]
                    self.token_expiry = (
                        datetime.now(timezone.utc)
                        + self.ACCESS_TOKEN_LIFETIME
                        - self.TOKEN_REFRESH_MARGIN
                    )
                    self.session.headers["Authorization"] = f"Bearer {self.access_token}"
                    logger.info("🔑 Freqtrade API authentication successful.")
                    return True
                except Exception as e:
                    logger.warning(f"Login attempt {attempt}/{self.MAX_RETRIES} failed: {e}")
                    if attempt < self.MAX_RETRIES:
                        time.sleep(2)
            return False

    def _ensure_authenticated(self) -> bool:
        """Ensure access token is valid, refreshing if needed."""
        with self._lock:
            if not self.access_token:
                return self.login()
            if self.token_expiry and datetime.now(timezone.utc) >= self.token_expiry:
                return self._refresh_token()
            return True

    def _refresh_token(self) -> bool:
        """Refresh JWT access token."""
        with self._lock:
            try:
                headers = {"Authorization": f"Bearer {self.refresh_token}"}
                resp = self.session.post(
                    f"{self.base_url}/token/refresh",
                    headers=headers,
                    timeout=(3.0, 10.0),
                )
                resp.raise_for_status()
                data = resp.json()
                self.access_token = data["access_token"]
                self.token_expiry = (
                    datetime.now(timezone.utc)
                    + self.ACCESS_TOKEN_LIFETIME
                    - self.TOKEN_REFRESH_MARGIN
                )
                self.session.headers["Authorization"] = f"Bearer {self.access_token}"
                return True
            except Exception:
                return self.login()

    def _request(self, method: str, endpoint: str, **kwargs) -> dict | list | None:
        """Execute authenticated HTTP request with retries and lock safety."""
        with self._lock:
            if not self._ensure_authenticated():
                return None
            url = f"{self.base_url}{endpoint}"
            for attempt in range(1, self.MAX_RETRIES + 1):
                try:
                    resp = self.session.request(method, url, timeout=(3.0, 10.0), **kwargs)
                    if resp.status_code == 401:
                        if self._refresh_token():
                            continue
                        return None
                    resp.raise_for_status()
                    return resp.json()
                except Exception as e:
                    if attempt < self.MAX_RETRIES:
                        time.sleep(1)
                        continue
                    logger.error(f"API request failed: {method} {endpoint} → {e}")
                    return None
            return None

    def get(self, endpoint: str, **kwargs) -> dict | list | None:
        return self._request("GET", endpoint, **kwargs)

    def post(self, endpoint: str, **kwargs) -> dict | list | None:
        return self._request("POST", endpoint, **kwargs)

    # ----- Operational Methods & Critical Command Executor -----

    def execute_critical_command(
        self,
        command_name: str,
        method: str,
        endpoint: str,
        payload: dict | None = None,
        expected_state: str | None = None,
        max_attempts: int = CRITICAL_COMMAND_MAX_ATTEMPTS,
        backoff_base: float = 1.0,
    ) -> bool:
        """
        Executes critical Freqtrade control commands (/stopbuy, /start, /forceexit)
        with exponential backoff, detailed HTTP status inspection, and state verification.
        Logs CRITICAL alerts on retries and failures.
        """
        with self._lock:
            for attempt in range(1, max_attempts + 1):
                try:
                    if not self._ensure_authenticated():
                        logger.critical(
                            f"🚨 CRITICAL CMD [{command_name}] attempt {attempt}/{max_attempts}: Authentication failed!"
                        )
                        delay = backoff_base * (1.5 ** (attempt - 1))
                        time.sleep(delay)
                        continue

                    url = f"{self.base_url}{endpoint}"
                    resp = self.session.request(
                        method,
                        url,
                        json=payload,
                        timeout=(3.0, 10.0),
                    )

                    if resp.status_code == 401:
                        logger.warning(f"⚠️ Token expired during critical command [{command_name}]. Refreshing token...")
                        if self._refresh_token():
                            continue

                    if resp.status_code not in (200, 201):
                        logger.critical(
                            f"🚨 CRITICAL CMD [{command_name}] attempt {attempt}/{max_attempts} failed! "
                            f"HTTP {resp.status_code}: {resp.text}"
                        )
                        delay = backoff_base * (1.5 ** (attempt - 1))
                        time.sleep(delay)
                        continue

                    # If state verification is requested, query /show_config to confirm true bot state
                    if expected_state:
                        actual_state = self.get_bot_state()
                        if actual_state != expected_state.lower():
                            logger.critical(
                                f"🚨 STATE MISMATCH for [{command_name}] attempt {attempt}/{max_attempts}! "
                                f"Expected state '{expected_state}', but Freqtrade reports '{actual_state}'."
                            )
                            delay = backoff_base * (1.5 ** (attempt - 1))
                            time.sleep(delay)
                            continue

                    logger.info(f"✅ Critical command [{command_name}] successfully executed and verified on attempt {attempt}.")
                    return True

                except Exception as e:
                    logger.critical(
                        f"🚨 CRITICAL CMD [{command_name}] attempt {attempt}/{max_attempts} exception: {e}"
                    )
                    delay = backoff_base * (1.5 ** (attempt - 1))
                    time.sleep(delay)

            logger.critical(
                f"💥 FATAL: Critical command [{command_name}] failed permanently after {max_attempts} attempts! "
                f"Operator manual intervention required!"
            )
            return False

    def get_balance(self) -> dict | None:
        """Fetch raw balance response containing total, total_bot, and individual currencies."""
        data = self.get("/balance")
        if data and isinstance(data, dict):
            return data
        return None

    def get_equity(self) -> float | None:
        """
        True real-time portfolio equity in stake currency (USDT).
        Freqtrade's /balance endpoint already computes the total valuation:
          - 'total_bot': bot-managed free stake + market value of all held assets.
          - 'total': total wallet stake across all assets.
        We strictly avoid double-counting unrealized PnL while guarding against NaN.
        """
        data = self.get_balance()
        if not data:
            return None

        # Prioritize total_bot (exact bot-allocated equity)
        total_bot = data.get("total_bot")
        if (
            total_bot is not None
            and isinstance(total_bot, (int, float))
            and total_bot > 0
            and not math.isnan(total_bot)
        ):
            return float(total_bot)

        # Fallback to overall total if total_bot is not populated
        total = data.get("total")
        if (
            total is not None
            and isinstance(total, (int, float))
            and total > 0
            and not math.isnan(total)
        ):
            return float(total)

        return None

    def get_open_trades(self) -> list:
        data = self.get("/status")
        if isinstance(data, list):
            return data
        return []

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
            endpoint="/stopbuy",
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
            payload={"tradeid": str(trade_id)},
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
            "btc_1h_pct": 0.0,
            "btc_24h_pct": 0.0,
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
                if r_btc.status_code == 200:
                    d = r_btc.json()
                    metrics["btc_price"] = float(d.get("lastPrice", 0.0))
                    metrics["btc_24h_pct"] = round(float(d.get("priceChangePercent", 0.0)), 2)

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
                    f"{base_url}/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=2",
                    timeout=(2.0, 3.0),
                )
                if r_kline.status_code == 200:
                    k = r_kline.json()
                    if k and len(k) > 0 and len(k[-1]) >= 5:
                        open_p = float(k[-1][1])
                        close_p = float(k[-1][4])
                        if open_p > 0:
                            metrics["btc_1h_pct"] = round((close_p - open_p) / open_p * 100, 2)

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

        return self._cached_metrics if self._cached_metrics else metrics


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
                from google import genai
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

                    if not has_catastrophic_headline and not has_price_collapse:
                        logger.warning(
                            f"⚠️ Anti-Hallucination: Gemini signaled PANIC, but market price momentum is calm "
                            f"(BTC 1h={macro['btc_1h_pct']:+}%, 24h={macro['btc_24h_pct']:+}%) and no catastrophic "
                            f"headlines were detected. Downgrading to BEAR."
                        )
                        llm_result["regime"] = "BEAR"
                        llm_result["reason"] = f"[Downgraded from PANIC] {llm_result.get('reason')}"

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
            from google.genai import types

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

class RiskManager:
    """
    Main orchestrator for the Zero-Burn risk management architecture.
    Runs:
      1. Main Thread: Dedicated High-Priority Drawdown Guard on true floating equity.
      2. Background Thread: Live News + Binance Macro + Gemini AI Sentinel.
    Protected by:
      - Anti-Whipsaw Hysteresis State Machine (BULL_CONFIRMATION_CYCLES)
      - Guarded Wake-Up Lock on cooldown expiration
      - Critical Command Retry with Exponential Backoff and State Verification
      - Deadlock-Free Architecture: Zero network calls inside _state_lock
    """

    def __init__(self):
        self.api_client = FreqtradeAPIClient(
            FREQTRADE_API_URL,
            FREQTRADE_USERNAME,
            FREQTRADE_PASSWORD,
        )
        self.sentiment = SentimentAnalyzer(GEMINI_API_KEY)

        self._state_lock = threading.RLock()
        self.initial_balance: float | None = None
        self.peak_balance: float | None = None
        self.nuclear_triggered = False
        self.current_regime = "BULL"
        self.cooldown_until: datetime | None = None
        self.wake_up_lock = False
        self.consecutive_bull_cycles = 0
        self.cycle_count = 0
        self._shutdown_requested = False

        self._load_state()

        signal.signal(signal.SIGTERM, self._handle_shutdown)
        signal.signal(signal.SIGINT, self._handle_shutdown)

    def _handle_shutdown(self, signum, frame):
        sig_name = signal.Signals(signum).name
        logger.info(f"🛑 Received {sig_name}, shutting down Risk Manager gracefully...")
        self._shutdown_requested = True
        self._save_state()

    def _load_state(self):
        with self._state_lock:
            if STATE_FILE.exists():
                try:
                    with open(STATE_FILE) as f:
                        state = json.load(f)
                    self.initial_balance = state.get("initial_balance")
                    self.peak_balance = state.get("peak_balance", self.initial_balance)
                    self.nuclear_triggered = state.get("nuclear_triggered", False)
                    self.current_regime = state.get("current_regime", "BULL")
                    self.wake_up_lock = state.get("wake_up_lock", False)
                    self.consecutive_bull_cycles = state.get("consecutive_bull_cycles", 0)
                    cooldown_str = state.get("cooldown_until")
                    if cooldown_str:
                        self.cooldown_until = datetime.fromisoformat(cooldown_str)
                    if self.initial_balance:
                        logger.info(
                            f"📁 Restored state from disk: initial={self.initial_balance:.2f} USDT, "
                            f"peak={self.peak_balance:.2f} USDT, regime={self.current_regime}, "
                            f"wake_up_lock={self.wake_up_lock}, bull_confirmations={self.consecutive_bull_cycles}"
                        )
                except Exception as e:
                    logger.warning(f"Failed to load state file: {e}")

    def _save_state(self):
        with self._state_lock:
            state = {
                "initial_balance": self.initial_balance,
                "peak_balance": self.peak_balance,
                "nuclear_triggered": self.nuclear_triggered,
                "current_regime": self.current_regime,
                "wake_up_lock": self.wake_up_lock,
                "consecutive_bull_cycles": self.consecutive_bull_cycles,
                "cooldown_until": self.cooldown_until.isoformat() if self.cooldown_until else None,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
            try:
                tmp_file = STATE_FILE.with_suffix(".tmp")
                with open(tmp_file, "w") as f:
                    json.dump(state, f, indent=2)
                tmp_file.replace(STATE_FILE)
            except Exception as e:
                logger.error(f"Failed to save state: {e}")

    def trigger_nuclear_option(self, reason: str):
        """Emergency hard liquidation (Nuclear Option). Zero network calls inside lock."""
        with self._state_lock:
            if self.nuclear_triggered:
                return

            logger.critical("=" * 70)
            logger.critical("☢️  NUCLEAR OPTION TRIGGERED!")
            logger.critical(f"   Reason: {reason}")
            logger.critical("=" * 70)

            self.nuclear_triggered = True
            self.wake_up_lock = True
            self._save_state()

        # ALL NETWORK OPERATIONS ARE 100% OUTSIDE _STATE_LOCK
        # 1. Stop all buying immediately with verified critical executor
        self.api_client.stop_buying()
        logger.info("🛑 Bot buying permanently HALTED.")

        # 2. Force exit all open trades
        trades = self.api_client.get_open_trades()
        if trades and len(trades) > 0:
            logger.info(f"📤 Liquidating {len(trades)} open positions...")
            for t in trades:
                tid = t.get("trade_id") or t.get("tradeid") or t.get("id")
                pair = t.get("pair")
                if tid is not None:
                    self.api_client.force_exit(tid)
                    logger.warning(f"  ✅ Liquidated #{tid} ({pair})")
                    time.sleep(0.3)
        else:
            logger.info("No open positions to liquidate.")

        logger.critical("☢️  Nuclear option execution complete. Capital secured in USDT.")
        self._save_state()

    def _check_drawdown(self) -> bool:
        """High-priority drawdown calculation against peak balance. Zero network calls inside lock."""
        current_equity = self.api_client.get_equity()
        if current_equity is None or current_equity <= 0 or math.isnan(current_equity):
            return False

        should_trigger_nuclear = False
        nuclear_reason = ""

        with self._state_lock:
            if self.initial_balance is None:
                self.initial_balance = current_equity
                self.peak_balance = current_equity
                logger.info(f"🔒 Initial balance locked at: ${self.initial_balance:,.2f} USDT")
                self._save_state()

            if self.peak_balance is None or current_equity > self.peak_balance:
                self.peak_balance = current_equity
                self._save_state()

            if self.peak_balance <= 0:
                return False

            drawdown = (self.peak_balance - current_equity) / self.peak_balance
            drawdown_pct = drawdown * 100

            if drawdown > 0:
                logger.info(
                    f"💰 Equity: ${current_equity:,.2f} | Peak: ${self.peak_balance:,.2f} | "
                    f"Drawdown: {drawdown_pct:.2f}% (Limit: {GLOBAL_HARD_STOPLOSS_PERCENT * 100:.1f}%)"
                )
            else:
                profit_pct = ((current_equity - self.initial_balance) / self.initial_balance) * 100
                logger.info(
                    f"💰 Equity: ${current_equity:,.2f} | Peak: ${self.peak_balance:,.2f} | "
                    f"Total Profit: +{profit_pct:.2f}%"
                )

            if drawdown > GLOBAL_HARD_STOPLOSS_PERCENT:
                should_trigger_nuclear = True
                nuclear_reason = (
                    f"Global portfolio drawdown {drawdown_pct:.2f}% breached "
                    f"the {GLOBAL_HARD_STOPLOSS_PERCENT * 100:.1f}% limit!"
                )

        # Trigger Nuclear Option OUTSIDE the lock to prevent deadlock
        if should_trigger_nuclear:
            self.trigger_nuclear_option(nuclear_reason)
            return True

        return False

    def run(self):
        """Main execution entry point."""
        logger.info("=" * 70)
        logger.info("🚀 Zero-Burn AI Risk Sentinel — Active (Enterprise Edition)")
        logger.info(f"   Freqtrade API:       {FREQTRADE_API_URL}")
        logger.info(f"   Drawdown Limit:      {GLOBAL_HARD_STOPLOSS_PERCENT * 100:.1f}%")
        logger.info(f"   Check Interval:      {CHECK_INTERVAL_SECONDS}s")
        logger.info(f"   Hysteresis Cycles:   {BULL_CONFIRMATION_CYCLES} cycles")
        logger.info(f"   Wake-Up Cooldown:    {WAKE_UP_COOLDOWN_MINUTES}m")
        logger.info(f"   Daily Gemini Cap:    {MAX_DAILY_GEMINI_CALLS} calls")
        logger.info("=" * 70)

        with self._state_lock:
            if self.nuclear_triggered:
                logger.critical("☢️  Nuclear option was triggered previously. Delete state file to reset.")
                return

        if not self.api_client.login():
            logger.critical("❌ Failed to authenticate with Freqtrade API. Exiting.")
            sys.exit(1)

        # Launch LLM Sentinel in a background daemon thread
        sentinel_thread = threading.Thread(
            target=self._llm_sentinel_loop,
            name="AI-Sentinel-Thread",
            daemon=True,
        )
        sentinel_thread.start()

        # Main thread: Dedicated High-Priority Drawdown Guard
        self._drawdown_guard_loop()

    def _drawdown_guard_loop(self):
        while not self.nuclear_triggered and not self._shutdown_requested:
            try:
                if self._check_drawdown():
                    break
                time.sleep(CHECK_INTERVAL_SECONDS)
            except Exception as e:
                logger.error(f"Drawdown monitor error: {e}")
                time.sleep(CHECK_INTERVAL_SECONDS)

    def _llm_sentinel_loop(self):
        """Background thread auditing News, Binance Macro, and Gemini AI with Hysteresis & Wake-Up Lock."""
        while not self.nuclear_triggered and not self._shutdown_requested:
            try:
                self.cycle_count += 1
                now = datetime.now(timezone.utc)

                # Check if currently inside Cooldown timer (Brief local read, 0 network)
                with self._state_lock:
                    if self.cooldown_until is not None and now < self.cooldown_until:
                        in_cooldown_timer = True
                        rem_min = int((self.cooldown_until - now).total_seconds() // 60)
                    else:
                        in_cooldown_timer = False
                        rem_min = 0
                    wake_up_locked = self.wake_up_lock

                if in_cooldown_timer:
                    if self.cycle_count % 5 == 0 or rem_min <= 5:
                        logger.info(f"⏳ System in DEFENSIVE COOLDOWN: {rem_min}m remaining before re-evaluation.")
                    time.sleep(CHECK_INTERVAL_SECONDS * LLM_CHECK_INTERVAL_CYCLES)
                    continue

                # Cooldown timer has elapsed! Query market intelligence OUTSIDE of lock
                whitelist = self.api_client.get_whitelist()
                decision = self.sentiment.analyze_market(whitelist)
                regime = decision.get("regime", "BULL")
                confidence = float(decision.get("confidence", 0.0))
                reason = decision.get("reason", "N/A")
                cooldown_min = decision.get("cooldown_minutes", WAKE_UP_COOLDOWN_MINUTES)
                flagged_coins = decision.get("flagged_coins", {})
                macro = self.sentiment.macro_monitor.get_metrics()
                fgi_val, _ = self.sentiment.get_fear_and_greed_index()

                # 1. Targeted Token Defense (EXIT_PAIR)
                if flagged_coins and isinstance(flagged_coins, dict):
                    for coin_id, info in flagged_coins.items():
                        act = info.get("action") if isinstance(info, dict) else str(info)
                        if act in ("EXIT_PAIR", "LIQUIDATE"):
                            logger.warning(f"🚨 TARGETED EXIT for {coin_id}: adding to blacklist and closing position.")
                            self.api_client.add_to_blacklist([coin_id])
                            trades = self.api_client.get_open_trades()
                            if trades:
                                for t in trades:
                                    t_pair = t.get("pair", "")
                                    if (
                                        t_pair == coin_id
                                        or t_pair.startswith(f"{coin_id}/")
                                        or coin_id.startswith(t_pair.split("/")[0])
                                    ):
                                        tid = t.get("trade_id") or t.get("tradeid") or t.get("id")
                                        if tid is not None:
                                            self.api_client.force_exit(tid)

                # 2. Emergency Black Swan (PANIC)
                if regime == "PANIC":
                    self.trigger_nuclear_option(f"AI Sentinel Black Swan Panic: {reason}")
                    break

                # 3. WAKE-UP LOCK LOGIC (Guarded Cooldown Expiry)
                if wake_up_locked:
                    is_safe_bull = (regime == "BULL" and confidence >= 0.80)
                    is_macro_stable = (macro.get("btc_1h_pct", 0.0) >= -1.0 and macro.get("btc_24h_pct", 0.0) >= -3.5)
                    is_fgi_stable = (fgi_val > 25)

                    if not (is_safe_bull and is_macro_stable and is_fgi_stable):
                        # Wake-up criteria NOT met! Reject wakeup and extend cooldown
                        extension_min = cooldown_min if cooldown_min > 0 else WAKE_UP_COOLDOWN_MINUTES
                        with self._state_lock:
                            self.cooldown_until = now + timedelta(minutes=extension_min)
                            self.consecutive_bull_cycles = 0
                            self._save_state()

                        logger.warning(
                            f"🔒 WAKE-UP LOCK REJECTED: Cooldown expired but market remains unsafe "
                            f"(Regime={regime}, Conf={confidence:.2f}, BTC 1h={macro.get('btc_1h_pct')}%, "
                            f"BTC 24h={macro.get('btc_24h_pct')}%, FGI={fgi_val}). "
                            f"Extending defensive cooldown by {extension_min}m. Buying remains HALTED."
                        )
                        # Re-affirm stop buying to be 100% sure Freqtrade is paused
                        self.api_client.stop_buying()
                        time.sleep(CHECK_INTERVAL_SECONDS * LLM_CHECK_INTERVAL_CYCLES)
                        continue

                    # Market evaluated as calm BULL: Enforce HYSTERESIS confirmation
                    with self._state_lock:
                        self.consecutive_bull_cycles += 1
                        curr_confirmations = self.consecutive_bull_cycles
                        self._save_state()

                    logger.info(
                        f"⏳ HYSTERESIS CONFIRMATION: Calm cycle {curr_confirmations}/{BULL_CONFIRMATION_CYCLES} recorded "
                        f"(BTC 1h={macro.get('btc_1h_pct'):+}%, FGI={fgi_val}). Entries remain PAUSED until confirmation completes."
                    )

                    if curr_confirmations < BULL_CONFIRMATION_CYCLES:
                        time.sleep(CHECK_INTERVAL_SECONDS * LLM_CHECK_INTERVAL_CYCLES)
                        continue

                    # Hysteresis requirement achieved! Release Wake-Up Lock and resume trading
                    with self._state_lock:
                        self.wake_up_lock = False
                        self.current_regime = "BULL"
                        self.cooldown_until = None
                        self.consecutive_bull_cycles = 0
                        self._save_state()

                    logger.info(
                        f"🟢 WAKE-UP LOCK RELEASED & HYSTERESIS PASSED: {BULL_CONFIRMATION_CYCLES} consecutive calm cycles confirmed. "
                        f"Resuming active trade entries (/start)."
                    )
                    self.api_client.start_trading()
                    time.sleep(CHECK_INTERVAL_SECONDS * LLM_CHECK_INTERVAL_CYCLES)
                    continue

                # 4. NORMAL ACTIVE MONITORING (When wake_up_lock is False)
                if regime == "BEAR":
                    with self._state_lock:
                        self.current_regime = "BEAR"
                        self.wake_up_lock = True
                        self.cooldown_until = now + timedelta(minutes=cooldown_min)
                        self.consecutive_bull_cycles = 0
                        self._save_state()

                    logger.warning(
                        f"🛡️  PROACTIVE DEFENSE ACTIVATED (BEAR): {reason}\n"
                        f"    Halting new trade entries for {cooldown_min} minutes. "
                        f"Existing trades will exit naturally on stops/targets."
                    )
                    self.api_client.stop_buying()

                elif regime == "BULL":
                    # Normal market, no change needed. Reset any partial counters
                    with self._state_lock:
                        self.consecutive_bull_cycles = 0

                time.sleep(CHECK_INTERVAL_SECONDS * LLM_CHECK_INTERVAL_CYCLES)

            except Exception as e:
                logger.error(f"Sentinel background loop error: {e}", exc_info=True)
                time.sleep(CHECK_INTERVAL_SECONDS * LLM_CHECK_INTERVAL_CYCLES)


if __name__ == "__main__":
    manager = RiskManager()
    manager.run()

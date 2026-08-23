#!/usr/bin/env python3
"""
Zero-Burn LLM Risk Manager — Phase 4
=====================================

External supervisor script that monitors Freqtrade via REST API
and uses AI + Sentiment analysis to detect Black Swan events.

Architecture:
    ┌────────────────┐   JWT Auth    ┌──────────────┐
    │  Risk Manager  │◄────────────► │  Freqtrade   │
    │  (this script) │               │  REST API    │
    └───────┬────────┘               └──────────────┘
            │
            ├── Gemini 2.5 Flash (primary sentiment)
            └── Fear & Greed Index  (deterministic fallback)

Usage:
    export FREQTRADE_USERNAME="freqtrade"
    export FREQTRADE_PASSWORD="SuperSecurePassword"
    export GEMINI_API_KEY="your_gemini_api_key"
    python scripts/llm_risk_manager.py
"""

import os
import sys
import time
import json
import signal
import logging
import requests
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

# ==================================================================================
# Logging Setup
# ==================================================================================

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Ensure log directory exists
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
# Configuration (all overridable via environment variables)
# ==================================================================================

FREQTRADE_API_URL = os.getenv("FREQTRADE_API_URL", "http://127.0.0.1:8080/api/v1")
FREQTRADE_USERNAME = os.getenv("FREQTRADE_USERNAME", "freqtrade")
FREQTRADE_PASSWORD = os.getenv("FREQTRADE_PASSWORD", "SuperSecurePassword")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Risk Thresholds
GLOBAL_HARD_STOPLOSS_PERCENT = float(os.getenv("GLOBAL_HARD_STOPLOSS_PERCENT", "0.05"))

# Timing
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
LLM_CHECK_INTERVAL_CYCLES = int(os.getenv("LLM_CHECK_INTERVAL_CYCLES", "5"))

# LLM Settings
LLM_TIMEOUT_MS = int(os.getenv("LLM_TIMEOUT_MS", "15000"))
LLM_MAX_CONSECUTIVE_FAILURES = int(os.getenv("LLM_MAX_CONSECUTIVE_FAILURES", "3"))
LLM_CIRCUIT_BREAKER_COOLDOWN_SECONDS = int(
    os.getenv("LLM_CIRCUIT_BREAKER_COOLDOWN_SECONDS", "1800")
)

# State persistence file
STATE_FILE = Path(__file__).resolve().parent / "risk_manager_state.json"


# ==================================================================================
# Freqtrade API Client
# ==================================================================================


class FreqtradeAPIClient:
    """
    Handles authenticated communication with the Freqtrade REST API.

    Implements the correct JWT authentication flow:
      1. Login with HTTP Basic Auth -> receive access_token + refresh_token
      2. Use access_token as Bearer token for API calls
      3. Auto-refresh access_token before expiry (15 min lifetime)
      4. Fall back to full re-login if refresh fails
    """

    ACCESS_TOKEN_LIFETIME = timedelta(minutes=15)
    TOKEN_REFRESH_MARGIN = timedelta(minutes=2)
    MAX_RETRIES = 3
    RETRY_BACKOFF_BASE = 2  # seconds

    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.token_expiry: Optional[datetime] = None
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    # ----- Authentication -----

    def login(self) -> bool:
        """Authenticate with Freqtrade API using HTTP Basic Auth."""
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                response = self.session.post(
                    f"{self.base_url}/token/login",
                    auth=(self.username, self.password),
                    timeout=10,
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
                logger.info("✅ Successfully authenticated with Freqtrade API")
                return True

            except requests.exceptions.ConnectionError as e:
                logger.warning(
                    f"Login attempt {attempt}/{self.MAX_RETRIES} — "
                    f"connection error: {e}"
                )
            except requests.exceptions.HTTPError as e:
                logger.error(
                    f"Login attempt {attempt}/{self.MAX_RETRIES} — HTTP error: {e}"
                )
                if e.response is not None and e.response.status_code == 401:
                    logger.critical(
                        "❌ Invalid credentials for Freqtrade API! "
                        "Check FREQTRADE_USERNAME and FREQTRADE_PASSWORD."
                    )
                    return False  # Don't retry on bad credentials
            except Exception as e:
                logger.error(
                    f"Login attempt {attempt}/{self.MAX_RETRIES} — "
                    f"unexpected error: {e}"
                )

            if attempt < self.MAX_RETRIES:
                backoff = self.RETRY_BACKOFF_BASE**attempt
                logger.info(f"Retrying login in {backoff}s...")
                time.sleep(backoff)

        logger.critical(
            "❌ Failed to authenticate with Freqtrade API after all retries"
        )
        return False

    def _refresh_access_token(self) -> bool:
        """Refresh the access token using the refresh token."""
        try:
            response = self.session.post(
                f"{self.base_url}/token/refresh",
                headers={"Authorization": f"Bearer {self.refresh_token}"},
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()
            self.access_token = data["access_token"]
            self.token_expiry = (
                datetime.now(timezone.utc)
                + self.ACCESS_TOKEN_LIFETIME
                - self.TOKEN_REFRESH_MARGIN
            )
            logger.info("🔄 Access token refreshed successfully")
            return True
        except Exception as e:
            logger.warning(f"Token refresh failed ({e}), attempting full re-login...")
            return self.login()

    def _ensure_authenticated(self) -> bool:
        """Ensure we have a valid, non-expired access token."""
        if self.access_token is None:
            return self.login()
        if self.token_expiry and datetime.now(timezone.utc) >= self.token_expiry:
            return self._refresh_access_token()
        return True

    # ----- Generic Request -----

    def _request(
        self, method: str, endpoint: str, retries: int = 2, **kwargs
    ) -> Optional[dict | list]:
        """
        Make an authenticated request to the Freqtrade API.
        Handles token expiry and 401 responses with automatic re-auth.
        """
        kwargs.setdefault("timeout", 10)

        for attempt in range(retries + 1):
            if not self._ensure_authenticated():
                return None

            try:
                # Build headers with current token
                headers = kwargs.pop("headers", {})
                headers["Authorization"] = f"Bearer {self.access_token}"

                response = self.session.request(
                    method,
                    f"{self.base_url}{endpoint}",
                    headers=headers,
                    **kwargs,
                )

                # If 401, force re-login and retry
                if response.status_code == 401 and attempt < retries:
                    logger.warning(
                        f"Got 401 on {method} {endpoint}, re-authenticating..."
                    )
                    self.access_token = None
                    continue

                response.raise_for_status()
                return response.json()

            except requests.exceptions.HTTPError as e:
                logger.error(f"API request failed: {method} {endpoint} → {e}")
                if attempt < retries:
                    time.sleep(1)
                    continue
                return None
            except requests.exceptions.ConnectionError as e:
                logger.error(f"Connection error to Freqtrade API: {e}")
                return None
            except requests.exceptions.Timeout as e:
                logger.error(f"Timeout on {method} {endpoint}: {e}")
                if attempt < retries:
                    time.sleep(1)
                    continue
                return None
            except Exception as e:
                logger.error(f"Unexpected API error on {method} {endpoint}: {e}")
                return None

        return None

    def get(self, endpoint: str, **kwargs) -> Optional[dict | list]:
        """Authenticated GET request."""
        return self._request("GET", endpoint, **kwargs)

    def post(self, endpoint: str, **kwargs) -> Optional[dict | list]:
        """Authenticated POST request."""
        return self._request("POST", endpoint, **kwargs)

    # ----- High-Level Operations -----

    def get_balance(self) -> Optional[float]:
        """Get total account balance from Freqtrade."""
        data = self.get("/balance")
        if data and isinstance(data, dict):
            return data.get("total", 0.0)
        return None

    def get_open_trades(self) -> Optional[list]:
        """Get list of currently open trades."""
        data = self.get("/status")
        if isinstance(data, list):
            return data
        return None

    def stop_buying(self) -> bool:
        """Halt all new entry signals."""
        result = self.post("/stopbuy")
        return result is not None

    def force_exit(self, trade_id: int) -> bool:
        """Force exit a specific trade at market price."""
        result = self.post("/forceexit", json={"tradeid": str(trade_id)})
        return result is not None

    def get_blacklist(self) -> list:
        """Get the current pair blacklist."""
        data = self.get("/blacklist")
        if data and isinstance(data, dict):
            return data.get("blacklist", [])
        return []

    def add_to_blacklist(self, pairs: list) -> bool:
        """Add specific pairs to the blacklist."""
        if not pairs:
            return True
        result = self.post("/blacklist", json={"blacklist": pairs})
        return result is not None


# ==================================================================================
# Sentiment Analyzer (Gemini + Fear & Greed Index)
# ==================================================================================

GEMINI_SYSTEM_PROMPT = """\
You are a cryptocurrency macro-risk sentinel for an automated trading system \
managing institutional capital. Your decisions directly control whether the \
system liquidates all positions.

YOUR SOLE JOB is to classify the CURRENT market regime into ONE of three categories:

1. **BULL** — Normal or positive conditions. No intervention needed.
2. **BEAR** — Elevated risk or nervous market. The system should reduce exposure.
3. **PANIC** — Extreme systemic threat detected (Black Swan). The system must \
IMMEDIATELY liquidate ALL positions and halt trading.

═══ PANIC CRITERIA (trigger ONLY for genuine catastrophic events) ═══

PANIC should be triggered for:
• Major exchange collapse or insolvency (FTX/Mt.Gox-level event)
• Government ban on cryptocurrency trading in a G7/G20 country
• Critical smart contract exploit draining billions from DeFi
• Nuclear/military conflict directly impacting global financial markets
• Stablecoin de-peg of USDT/USDC below $0.95
• Central bank emergency actions causing market-wide circuit breakers

PANIC should NOT be triggered for:
• Normal 10-30% market corrections (these are routine in crypto)
• FUD about regulations that haven't been enacted yet
• Individual altcoin crashes (unless top-5 by market cap)
• Social media rumors without official confirmation
• Gradual bear market declines over weeks/months
• Exchange temporary outages or maintenance

═══ OUTPUT FORMAT ═══

Respond with ONLY a valid JSON object. No markdown, no explanation, no preamble:
{"regime": "BULL", "confidence": 0.85, "reason": "brief one-line explanation"}

The "regime" field must be exactly one of: BULL, BEAR, PANIC (uppercase).
The "confidence" field must be a float between 0.0 and 1.0.
"""

MICRO_SYSTEM_PROMPT = """\
You are a Senior Crypto Trader managing risk for an automated system.
You will be given a list of cryptocurrency pairs that the system is currently trading.
YOUR JOB is to check for specific, devastating news or structural flaws regarding ONLY these coins.

Categories of risk for a single coin:
- HODL: Normal market conditions, low-level rumors, general volatility. No action needed.
- EXIT_PAIR: Confirmed catastrophic news for this specific coin (e.g., smart contract hacked, founders arrested, major exchange delisting, SEC lawsuit). The system must dump this coin immediately.
- CONTAGION: The catastrophic news for this coin will crash the entire global crypto market (e.g., USDT/USDC depegs, major exchange bankruptcy, Bitcoin critical flaw). The system must trigger the nuclear option.

OUTPUT FORMAT: Respond with ONLY a valid JSON object. No markdown, no explanation.
{
  "PAIR_NAME": {"action": "HODL"|"EXIT_PAIR"|"CONTAGION", "reason": "brief explanation"},
  ...
}
"""


class SentimentAnalyzer:
    """
    Analyzes market sentiment using a layered approach:

      1. Fear & Greed Index (always fetched first — fast, deterministic)
      2. Gemini LLM (primary analysis — nuanced, context-aware)
      3. Fallback logic if LLM is unavailable

    Circuit breaker: after N consecutive LLM failures, automatically
    falls back to FGI-only mode for a configurable cooldown period.
    """

    FGI_API_URL = "https://api.alternative.me/fng/"

    def __init__(self, gemini_api_key: str):
        self.gemini_api_key = gemini_api_key
        self.gemini_client = None
        self.consecutive_llm_failures = 0
        self.consecutive_micro_failures = 0
        self.circuit_breaker_until: Optional[datetime] = None

        if gemini_api_key:
            try:
                from google import genai

                self.gemini_client = genai.Client(
                    api_key=gemini_api_key,
                    http_options={"timeout": LLM_TIMEOUT_MS},
                )
                logger.info("✅ Gemini AI client initialized (model: gemini-2.5-flash)")
            except ImportError:
                logger.warning(
                    "⚠️ google-genai library not installed. "
                    "Run: pip install google-genai"
                )
            except Exception as e:
                logger.error(f"Failed to initialize Gemini client: {e}")
        else:
            logger.warning(
                "⚠️ GEMINI_API_KEY not set. Using FGI-only mode."
            )

    # ----- Fear & Greed Index -----

    def get_fear_and_greed_index(self) -> Optional[Tuple[int, str]]:
        """
        Fetch the current Crypto Fear & Greed Index from alternative.me.
        Returns: (value 0-100, classification_string) or None on failure.
        """
        try:
            response = requests.get(self.FGI_API_URL, timeout=10)
            response.raise_for_status()
            data = response.json()
            if "data" in data and len(data["data"]) > 0:
                entry = data["data"][0]
                value = int(entry["value"])
                classification = entry.get("value_classification", "Unknown")
                logger.info(f"📊 Fear & Greed Index: {value} ({classification})")
                return value, classification
        except requests.exceptions.Timeout:
            logger.warning("⚠️ Fear & Greed Index API timeout")
        except Exception as e:
            logger.error(f"Failed to fetch Fear & Greed Index: {e}")
        return None

    @staticmethod
    def _fgi_to_regime(fgi_value: int) -> str:
        """
        Convert Fear & Greed Index to market regime (deterministic fallback).

        Thresholds:
          FGI <= 10  →  panic  (Extreme Fear, potential systemic crisis)
          FGI <= 25  →  bear   (Fear, elevated risk)
          FGI >  25  →  bull   (Neutral to Greed, normal operations)
        """
        if fgi_value <= 10:
            return "panic"
        elif fgi_value <= 25:
            return "bear"
        else:
            return "bull"

    # ----- Circuit Breaker -----

    def _is_circuit_breaker_active(self) -> bool:
        """Check if the LLM circuit breaker is currently active."""
        if self.circuit_breaker_until is None:
            return False
        if datetime.now(timezone.utc) >= self.circuit_breaker_until:
            logger.info(
                "🔄 LLM circuit breaker cooldown expired. "
                "Re-enabling LLM analysis."
            )
            self.circuit_breaker_until = None
            self.consecutive_llm_failures = 0
            return False
        remaining = (self.circuit_breaker_until - datetime.now(timezone.utc)).seconds
        logger.debug(
            f"Circuit breaker active, {remaining}s remaining until LLM re-enabled"
        )
        return True

    def _trip_circuit_breaker(self):
        """Activate the circuit breaker after too many LLM failures."""
        self.circuit_breaker_until = datetime.now(timezone.utc) + timedelta(
            seconds=LLM_CIRCUIT_BREAKER_COOLDOWN_SECONDS
        )
        cooldown_min = LLM_CIRCUIT_BREAKER_COOLDOWN_SECONDS / 60
        logger.warning(
            f"⚡ LLM circuit breaker TRIPPED after "
            f"{self.consecutive_llm_failures} consecutive failures. "
            f"Falling back to FGI-only mode for {cooldown_min:.0f} minutes."
        )

    # ----- Gemini LLM -----

    def _query_gemini(self, fgi_value: int, fgi_classification: str) -> Optional[str]:
        """
        Query Gemini 2.5 Flash for market regime analysis.

        Returns: regime string ("bull", "bear", "panic") or None on failure.
        """
        if not self.gemini_client:
            return None

        if self._is_circuit_breaker_active():
            return None

        try:
            from google.genai import types

            user_prompt = (
                f"Current Crypto Fear & Greed Index: {fgi_value} "
                f"({fgi_classification})\n\n"
                f"Current UTC time: "
                f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}\n\n"
                f"Based on the current market data and any knowledge you have "
                f"of recent events, classify the current market regime."
            )

            response = self.gemini_client.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=GEMINI_SYSTEM_PROMPT,
                    temperature=0.1,
                    max_output_tokens=256,
                    response_mime_type="application/json",
                ),
            )

            if not response.text:
                raise ValueError("Empty response from Gemini (possibly blocked by safety filters)")
            result_text = response.text.strip()
            result = json.loads(result_text)
            regime = result.get("regime", "").upper()
            confidence = float(result.get("confidence", 0.0))
            reason = result.get("reason", "N/A")

            regime_lower = regime.lower()
            if regime_lower not in ("bull", "bear", "panic"):
                logger.warning(
                    f"Invalid regime from Gemini: '{regime}'. Ignoring response."
                )
                raise ValueError(f"Invalid regime: {regime}")

            logger.info(
                f"🤖 Gemini analysis: regime={regime}, "
                f"confidence={confidence:.2f}, reason='{reason}'"
            )

            # Reset failure counter on success
            self.consecutive_llm_failures = 0
            return regime_lower

        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse Gemini response as JSON: {e}")
        except ImportError:
            logger.error("google-genai types module not available")
        except Exception as e:
            logger.warning(f"Gemini API call failed: {type(e).__name__}: {e}")

        # Track failure
        self.consecutive_llm_failures += 1
        logger.info(
            f"LLM failure count: {self.consecutive_llm_failures}/"
            f"{LLM_MAX_CONSECUTIVE_FAILURES}"
        )
        if self.consecutive_llm_failures >= LLM_MAX_CONSECUTIVE_FAILURES:
            self._trip_circuit_breaker()

        return None

    # ----- Main Analysis -----

    def analyze(self) -> str:
        """
        Determine the current market regime using the full analysis chain:

          1. Fetch Fear & Greed Index (fast, always available)
          2. Query Gemini LLM with FGI context (nuanced analysis)
          3. Fall back to FGI-only if LLM is unavailable
          4. Default to "bull" (safe default) if everything fails

        Anti-hallucination safety: if Gemini says PANIC but FGI is in
        greed territory (> 50), the signal is downgraded to BEAR.

        Returns: "bull", "bear", or "panic"
        """
        # Step 1: Get Fear & Greed Index
        fgi_result = self.get_fear_and_greed_index()

        if fgi_result is None:
            logger.warning(
                "⚠️ FGI unavailable. Attempting LLM-only analysis..."
            )
            # Try Gemini without FGI context
            gemini_regime = self._query_gemini(50, "Neutral (FGI unavailable)")
            if gemini_regime is not None:
                return gemini_regime
            logger.warning(
                "⚠️ Both FGI and LLM unavailable. "
                "Defaulting to 'bull' (safe default — no action taken)."
            )
            return "bull"

        fgi_value, fgi_classification = fgi_result

        # Step 2: Try Gemini LLM with FGI context
        gemini_regime = self._query_gemini(fgi_value, fgi_classification)

        if gemini_regime is not None:
            # Anti-hallucination guard: if LLM says PANIC but FGI is > 50,
            # the LLM might be hallucinating. Downgrade to BEAR.
            if gemini_regime == "panic" and fgi_value > 50:
                logger.warning(
                    f"⚠️ ANTI-HALLUCINATION: Gemini says PANIC but "
                    f"FGI is {fgi_value} (greed territory). "
                    f"Downgrading to BEAR as safety measure."
                )
                return "bear"
            return gemini_regime

        # Step 3: Fallback to deterministic FGI
        regime = self._fgi_to_regime(fgi_value)
        logger.info(
            f"📊 Using FGI deterministic fallback: "
            f"FGI={fgi_value} → regime={regime.upper()}"
        )
        return regime

    def analyze_specific_pairs(self, pairs: list) -> Optional[dict]:
        """
        Query Gemini to analyze specific crypto pairs using Senior Trader logic.
        """
        if not self.gemini_client or not pairs:
            return None

        if self._is_circuit_breaker_active():
            return None

        try:
            from google.genai import types

            pairs_str = ", ".join(pairs)
            user_prompt = (
                f"Analyze the following specific pairs currently in open trades: {pairs_str}. "
                f"Current UTC time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
            )

            response = self.gemini_client.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=MICRO_SYSTEM_PROMPT,
                    temperature=0.1,
                    max_output_tokens=512,
                    response_mime_type="application/json",
                ),
            )

            if not response.text:
                raise ValueError("Empty response from Gemini (possibly blocked by safety filters)")
            result_text = response.text.strip()
            result = json.loads(result_text)
            logger.info(f"🧠 Micro-analysis for {pairs_str}: {json.dumps(result)}")
            
            self.consecutive_micro_failures = 0
            return result

        except Exception as e:
            logger.warning(f"Gemini Micro-analysis failed: {type(e).__name__}: {e}")
            self.consecutive_micro_failures += 1
            if self.consecutive_micro_failures >= LLM_MAX_CONSECUTIVE_FAILURES:
                self._trip_circuit_breaker()
            return None


# ==================================================================================
# Risk Manager (Main Orchestrator)
# ==================================================================================


class RiskManager:
    """
    Main orchestrator for the Zero-Burn risk management system.

    Monitors:
      1. Global portfolio drawdown (hard mathematical stop — EVERY cycle)
      2. Market regime via AI + sentiment (Black Swan detection — every N cycles)

    Actions:
      - BULL regime: Normal operations, no intervention.
      - BEAR regime: Log warning. (Future: dynamically reduce max_open_trades)
      - PANIC regime: Trigger Nuclear Option.
      - Drawdown > threshold: Trigger Nuclear Option.
    """

    def __init__(self):
        self.api_client = FreqtradeAPIClient(
            FREQTRADE_API_URL,
            FREQTRADE_USERNAME,
            FREQTRADE_PASSWORD,
        )
        self.sentiment = SentimentAnalyzer(GEMINI_API_KEY)

        self.initial_balance: Optional[float] = None
        self.peak_balance: Optional[float] = None
        self.nuclear_triggered = False
        self.current_regime = "bull"
        self.cycle_count = 0
        self._shutdown_requested = False
        self.known_open_pairs = set()
        self.last_micro_analysis_time = None

        # Load persisted state from disk
        self._load_state()

        # Register signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, self._handle_shutdown)
        signal.signal(signal.SIGINT, self._handle_shutdown)

    # ----- Signal Handlers -----

    def _handle_shutdown(self, signum, frame):
        """Handle graceful shutdown on SIGTERM / SIGINT."""
        sig_name = signal.Signals(signum).name
        logger.info(f"🛑 Received {sig_name}, shutting down gracefully...")
        self._shutdown_requested = True
        self._save_state()

    # ----- State Persistence -----

    def _load_state(self):
        """Load persisted state from disk (survives restarts)."""
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, "r") as f:
                    state = json.load(f)
                self.initial_balance = state.get("initial_balance")
                self.peak_balance = state.get("peak_balance", self.initial_balance)
                self.nuclear_triggered = state.get("nuclear_triggered", False)
                self.current_regime = state.get("current_regime", "bull")
                if self.initial_balance:
                    logger.info(
                        f"📁 Restored state from disk: "
                        f"initial_balance={self.initial_balance:.2f} USDT, "
                        f"peak_balance={self.peak_balance:.2f} USDT, "
                        f"nuclear={self.nuclear_triggered}, "
                        f"regime={self.current_regime}"
                    )
                if self.nuclear_triggered:
                    logger.warning(
                        "☢️  Nuclear option was previously triggered. "
                        "Delete risk_manager_state.json to reset."
                    )
            except Exception as e:
                logger.warning(f"Failed to load state file: {e}")

    def _save_state(self):
        """Persist current state to disk."""
        state = {
            "initial_balance": self.initial_balance,
            "peak_balance": self.peak_balance,
            "nuclear_triggered": self.nuclear_triggered,
            "current_regime": self.current_regime,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    # ----- Nuclear Option -----

    def trigger_nuclear_option(self, reason: str):
        """
        EMERGENCY STOP — Last line of defense against catastrophic losses.

        Sequence:
          1. Halt all new entries (/stopbuy)
          2. Fetch all open trades (/status)
          3. Force exit each trade at market price (/forceexit)
          4. Persist state and enter hibernation
        """
        if self.nuclear_triggered:
            logger.warning("Nuclear option already triggered. Skipping.")
            return

        logger.critical("")
        logger.critical("=" * 60)
        logger.critical("☢️  NUCLEAR OPTION TRIGGERED!")
        logger.critical(f"   Reason: {reason}")
        logger.critical("=" * 60)
        logger.critical("")

        self.nuclear_triggered = True
        self._save_state()

        # Step 1: Stop all new entries IMMEDIATELY
        try:
            if self.api_client.stop_buying():
                logger.info("🛑 Bot buying HALTED successfully.")
            else:
                logger.error(
                    "⚠️ Failed to halt buying via API! "
                    "Continuing with force exits..."
                )
        except Exception as e:
            logger.error(f"Error halting buying: {e}")

        # Step 2: Force exit all open positions
        try:
            trades = self.api_client.get_open_trades()
            if trades and len(trades) > 0:
                logger.info(f"📤 Force exiting {len(trades)} open trade(s)...")
                exit_successes = 0
                exit_failures = 0
                for trade in trades:
                    trade_id = trade.get("trade_id")
                    pair = trade.get("pair", "unknown")
                    profit_pct = trade.get("profit_pct", 0.0)
                    try:
                        if self.api_client.force_exit(trade_id):
                            logger.warning(
                                f"  ✅ Force exited #{trade_id} "
                                f"({pair}, P/L: {profit_pct:.2f}%)"
                            )
                            exit_successes += 1
                        else:
                            logger.error(
                                f"  ❌ Failed to force exit #{trade_id} ({pair})"
                            )
                            exit_failures += 1
                    except Exception as e:
                        logger.error(
                            f"  ❌ Error force exiting #{trade_id}: {e}"
                        )
                        exit_failures += 1
                    # Small delay between exits to avoid rate limits
                    time.sleep(0.5)

                logger.info(
                    f"Initial force exit sweep complete: "
                    f"{exit_successes} succeeded, {exit_failures} failed"
                )

                # Retry sweep for failed exits
                MAX_RETRY_SWEEPS = 3
                RETRY_DELAY = 5  # seconds

                for sweep in range(MAX_RETRY_SWEEPS):
                    if exit_failures == 0:
                        break
                    
                    logger.warning(
                        f"⚠️ Retry sweep #{sweep + 1}/{MAX_RETRY_SWEEPS} "
                        f"for {exit_failures} failed exit(s)..."
                    )
                    time.sleep(RETRY_DELAY * (sweep + 1))
                    
                    trades = self.api_client.get_open_trades()
                    if not trades:
                        logger.info("All trades successfully closed.")
                        break
                    
                    exit_failures = 0
                    for trade in trades:
                        trade_id = trade.get("trade_id")
                        try:
                            if not self.api_client.force_exit(trade_id):
                                exit_failures += 1
                        except Exception:
                            exit_failures += 1
                        time.sleep(0.5)

                if exit_failures > 0:
                    logger.critical(
                        f"🚨 CRITICAL: {exit_failures} trade(s) could NOT be closed! "
                        f"MANUAL INTERVENTION REQUIRED on Binance!"
                    )

            else:
                logger.info("No open trades to close.")
        except Exception as e:
            logger.error(f"Error during force exit sweep: {e}")

        logger.critical(
            "☢️  Nuclear option execution complete. "
            "All funds should now be in USDT. "
            "Risk Manager entering hibernation."
        )
        self._save_state()

    # ----- Drawdown Check -----

    def _check_drawdown(self) -> bool:
        """
        Check global portfolio drawdown against the hard stop threshold.

        Returns True if nuclear option was triggered, False otherwise.
        """
        current_balance = self.api_client.get_balance()

        if current_balance is None:
            logger.warning(
                "⚠️ Could not fetch balance from Freqtrade. "
                "Skipping drawdown check this cycle."
            )
            return False

        # Lock initial balance on first successful read
        if self.initial_balance is None and current_balance > 0:
            self.initial_balance = current_balance
            self.peak_balance = current_balance
            logger.info(
                f"🔒 Initial balance locked at: "
                f"{self.initial_balance:.2f} USDT"
            )
            self._save_state()

        if self.initial_balance is None or self.initial_balance <= 0:
            return False

        # Update peak (high-water mark)
        if self.peak_balance is None:
            self.peak_balance = self.initial_balance
            
        if current_balance > self.peak_balance:
            self.peak_balance = current_balance
            self._save_state()

        # Calculate drawdown from PEAK
        drawdown = (self.peak_balance - current_balance) / self.peak_balance
        drawdown_pct = drawdown * 100

        if drawdown > 0:
            logger.info(
                f"💰 Balance: {current_balance:.2f} USDT | "
                f"Peak: {self.peak_balance:.2f} USDT | "
                f"Drawdown: {drawdown_pct:.2f}% "
                f"(threshold: {GLOBAL_HARD_STOPLOSS_PERCENT * 100:.1f}%)"
            )
        else:
            profit_pct = abs((self.initial_balance - current_balance) / self.initial_balance) * 100
            logger.info(
                f"💰 Balance: {current_balance:.2f} USDT | "
                f"Peak: {self.peak_balance:.2f} USDT | "
                f"Profit: +{profit_pct:.2f}% from initial"
            )

        if drawdown > GLOBAL_HARD_STOPLOSS_PERCENT:
            self.trigger_nuclear_option(
                f"Global drawdown {drawdown_pct:.2f}% exceeded "
                f"threshold {GLOBAL_HARD_STOPLOSS_PERCENT * 100:.1f}%!"
            )
            return True

        return False

    # ----- Main Loop -----

    def run(self):
        """
        Main execution loop.

        Runs indefinitely until:
          - Nuclear option is triggered
          - A shutdown signal (SIGTERM/SIGINT) is received
          - The process is killed externally
        """
        logger.info("")
        logger.info("=" * 60)
        logger.info("🚀 Zero-Burn LLM Risk Manager — Starting")
        logger.info(f"   Freqtrade API:       {FREQTRADE_API_URL}")
        logger.info(f"   Drawdown Threshold:  {GLOBAL_HARD_STOPLOSS_PERCENT * 100:.1f}%")
        logger.info(f"   Check Interval:      {CHECK_INTERVAL_SECONDS}s")
        logger.info(f"   LLM Check Every:     {LLM_CHECK_INTERVAL_CYCLES} cycles "
                     f"({LLM_CHECK_INTERVAL_CYCLES * CHECK_INTERVAL_SECONDS}s)")
        logger.info(f"   Gemini LLM:          "
                     f"{'Enabled' if self.sentiment.gemini_client else 'Disabled (FGI-only)'}")
        logger.info(f"   State File:          {STATE_FILE}")
        logger.info("=" * 60)
        logger.info("")

        # Abort if nuclear was already triggered in a previous run
        if self.nuclear_triggered:
            logger.critical(
                "☢️  Nuclear option was triggered in a previous session. "
                "The Risk Manager will NOT restart monitoring."
            )
            logger.critical(
                "   To reset, delete the state file: "
                f"rm {STATE_FILE}"
            )
            return

        # Initial login — abort if we can't authenticate
        if not self.api_client.login():
            logger.critical(
                "❌ Cannot start: failed to authenticate with Freqtrade API. "
                "Ensure Freqtrade is running and credentials are correct."
            )
            sys.exit(1)

        logger.info("🟢 Risk Manager is now active and monitoring.")

        # Start LLM Sentinel in a separate daemon thread
        llm_thread = threading.Thread(
            target=self._llm_sentinel_loop,
            name="LLM-Sentinel",
            daemon=True
        )
        llm_thread.start()

        # Main thread: ONLY drawdown guard (never blocked by LLM)
        self._drawdown_guard_loop()

    def _drawdown_guard_loop(self):
        """
        High-priority drawdown monitor. Runs every 20 seconds.
        NEVER calls any external LLM or sentiment API.
        """
        DRAWDOWN_CHECK_INTERVAL = CHECK_INTERVAL_SECONDS
        
        while not self.nuclear_triggered and not self._shutdown_requested:
            try:
                if self._check_drawdown():
                    break
                time.sleep(DRAWDOWN_CHECK_INTERVAL)
            except Exception as e:
                logger.error(f"Drawdown guard error: {e}", exc_info=True)
                time.sleep(DRAWDOWN_CHECK_INTERVAL)

    def _llm_sentinel_loop(self):
        """
        Lower-priority LLM analysis. Runs in background thread.
        If it blocks on Gemini, the drawdown guard is NOT affected.
        """
        LLM_INTERVAL = CHECK_INTERVAL_SECONDS * LLM_CHECK_INTERVAL_CYCLES
        
        while not self.nuclear_triggered and not self._shutdown_requested:
            try:
                self.cycle_count += 1
                logger.debug(f"--- Sentinel Cycle #{self.cycle_count} ---")

                # Smart Reactive Micro-Analysis for specific pairs
                trades = self.api_client.get_open_trades()
                if trades is not None:
                    current_pairs = {trade.get("pair") for trade in trades if trade.get("pair")}
                    
                    # Trigger if there are NEW pairs, or if 15 mins (approx 15 cycles) have passed
                    new_pairs = current_pairs - self.known_open_pairs
                    
                    if self.last_micro_analysis_time:
                        time_since_last = (datetime.now(timezone.utc) - self.last_micro_analysis_time).total_seconds()
                    else:
                        time_since_last = float('inf')
                    
                    if new_pairs or (current_pairs and time_since_last > 900): # 900s = 15m
                        if new_pairs:
                            logger.info(f"🆕 Detected new open pairs: {new_pairs}. Triggering immediate micro-analysis.")
                        else:
                            logger.info(f"⏱️ 15 minutes passed. Triggering routine micro-analysis for {current_pairs}.")
                        
                        micro_result = self.sentiment.analyze_specific_pairs(list(current_pairs))
                        self.last_micro_analysis_time = datetime.now(timezone.utc)
                        self.known_open_pairs = current_pairs
                        
                        if micro_result:
                            for pair, analysis in micro_result.items():
                                action = analysis.get("action", "HODL")
                                reason = analysis.get("reason", "N/A")
                                
                                if action == "CONTAGION":
                                    self.trigger_nuclear_option(f"CONTAGION risk detected for {pair}: {reason}")
                                    break
                                elif action == "EXIT_PAIR":
                                    logger.warning(f"🚨 EXIT_PAIR for {pair}: {reason}. Initiating force exit and blacklist.")
                                    # Find trade IDs for this pair
                                    trade_ids_to_close = [t.get("trade_id") for t in trades if t.get("pair") == pair]
                                    for tid in trade_ids_to_close:
                                        self.api_client.force_exit(tid)
                                    # Add to blacklist
                                    self.api_client.add_to_blacklist([pair])
                                elif action == "HODL":
                                    logger.debug(f"HODL {pair}: {reason}")
                                    
                    # Update known pairs even if we didn't query (e.g. if pairs closed)
                    self.known_open_pairs = current_pairs

                # Check market regime periodically
                if self.cycle_count % LLM_CHECK_INTERVAL_CYCLES == 0:
                    regime = self.sentiment.analyze()
                    previous_regime = self.current_regime
                    self.current_regime = regime
                    self._save_state()

                    if regime != previous_regime:
                        logger.info(
                            f"📡 Regime change detected: "
                            f"{previous_regime.upper()} → {regime.upper()}"
                        )

                    if regime == "panic":
                        self.trigger_nuclear_option(
                            "AI Sentinel detected extreme Black Swan / panic event!"
                        )
                        break
                    elif regime == "bear":
                        logger.warning("🐻 Market regime: BEAR — Elevated risk. Monitoring closely.")
                    elif regime == "bull":
                        logger.info("🟢 Market regime: BULL — Normal operations.")

                # Sleep until next check (for Sentinel, we sleep shorter so it can react quicker to shutdown)
                time.sleep(CHECK_INTERVAL_SECONDS)

            except Exception as e:
                logger.error(
                    f"LLM Sentinel loop error: {e}", exc_info=True
                )
                time.sleep(CHECK_INTERVAL_SECONDS)

        # ---- Shutdown ----
        if self._shutdown_requested:
            logger.info("🛑 Risk Manager stopped by user signal.")
        elif self.nuclear_triggered:
            logger.critical(
                "☢️  Risk Manager in HIBERNATION mode "
                "(nuclear triggered)."
            )

        self._save_state()
        logger.info("Risk Manager process ended.")


# ==================================================================================
# Entry Point
# ==================================================================================

if __name__ == "__main__":
    manager = RiskManager()
    manager.run()

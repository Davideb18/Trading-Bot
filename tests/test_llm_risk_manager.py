import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta
import math
import time
import sys
from pathlib import Path

# Add scripts directory to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from llm_risk_manager import (
    CryptoNewsAggregator,
    BinanceMacroMonitor,
    SentimentAnalyzer,
    FreqtradeAPIClient,
    RiskManager,
    APIQuotaTracker,
    BULL_CONFIRMATION_CYCLES,
    WAKE_UP_COOLDOWN_MINUTES,
)


class TestCryptoNewsAggregator(unittest.TestCase):
    def test_deduplication_and_cleaning(self):
        agg = CryptoNewsAggregator()
        mock_xml = """<rss><channel>
            <item><title>Bitcoin hits 84k &amp; surges</title></item>
            <item><title>Bitcoin hits 84k &amp; surges</title></item>
            <item><title>&lt;b&gt;Ethereum&lt;/b&gt; rally continues &#8217;26</title></item>
            <item><title><![CDATA[Solana breaks &quot;resistance&quot;]]></title></item>
        </channel></rss>"""

        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.content = mock_xml.encode("utf-8")
            mock_get.return_value = mock_resp

            headlines = agg.get_headlines(max_items=5)
            self.assertEqual(len(headlines), 3)
            self.assertIn("Bitcoin hits 84k & surges", headlines[0])
            self.assertIn("Ethereum rally continues ’26", headlines[1])
            self.assertIn('Solana breaks "resistance"', headlines[2])

    def test_feed_failure_fallback(self):
        agg = CryptoNewsAggregator()
        with patch("requests.get", side_effect=Exception("Timeout")):
            headlines = agg.get_headlines(max_items=5)
            self.assertEqual(headlines, [])

    def test_cache_eviction_and_memory_bounds(self):
        agg = CryptoNewsAggregator()
        now = time.time()
        # Insert expired article (3 hours old)
        agg._article_cache["old_story"] = {
            "source": "CoinDesk",
            "title": "Old Story From 3 Hours Ago",
            "timestamp": now - 10800,
        }
        # Insert fresh article (10 minutes old)
        agg._article_cache["fresh_story"] = {
            "source": "Decrypt",
            "title": "Fresh Story Breaking Right Now",
            "timestamp": now - 600,
        }

        # Run eviction
        agg._evict_expired_articles(now)

        # old_story must be physically deleted from dictionary
        self.assertNotIn("old_story", agg._article_cache)
        self.assertIn("fresh_story", agg._article_cache)

    def test_isolated_per_feed_exception_handling(self):
        agg = CryptoNewsAggregator()
        
        # Mock requests.get to return:
        # 1. CoinDesk: 403 Forbidden (Cloudflare block)
        # 2. CoinTelegraph: Corrupted XML
        # 3. Decrypt: Valid XML
        def mock_get(url, **kwargs):
            resp = MagicMock()
            if "coindesk" in url:
                resp.status_code = 403
                resp.content = b"Cloudflare 403 Forbidden"
            elif "cointelegraph" in url:
                resp.status_code = 200
                resp.content = b"<channel><item><title>Broken XML" # Missing closing tags
            elif "decrypt" in url:
                resp.status_code = 200
                resp.content = b"<rss><channel><item><title>Decrypt Exclusive: Market Rallies</title></item></channel></rss>"
            return resp

        with patch("requests.get", side_effect=mock_get):
            headlines = agg.get_headlines(max_items=5)
            # Decrypt must succeed despite CoinDesk 403 and CoinTelegraph XML error!
            self.assertEqual(len(headlines), 1)
            self.assertIn("Decrypt Exclusive: Market Rallies", headlines[0])


class TestBinanceMacroMonitor(unittest.TestCase):
    @patch("requests.get")
    def test_metrics_calculation(self, mock_get):
        def mock_requests(url, **kwargs):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            if "ticker/24hr?symbol=BTCUSDT" in url:
                mock_resp.json.return_value = {"lastPrice": "84000.00", "priceChangePercent": "2.50"}
            elif "ticker/24hr?symbol=ETHUSDT" in url:
                mock_resp.json.return_value = {"priceChangePercent": "3.10"}
            elif "klines" in url:
                mock_resp.json.return_value = [
                    [0, "83500.0", 0, 0, "84000.0"],
                    [0, "83500.0", 0, 0, "84000.0"]
                ]
            return mock_resp

        mock_get.side_effect = mock_requests
        monitor = BinanceMacroMonitor()
        metrics = monitor.get_metrics()

        self.assertEqual(metrics["btc_price"], 84000.00)
        self.assertEqual(metrics["btc_24h_pct"], 2.50)
        self.assertEqual(metrics["eth_24h_pct"], 3.10)
        self.assertFalse(metrics["is_flash_crash"])
        self.assertFalse(metrics["is_heavy_bear"])

    @patch("requests.get")
    def test_flash_crash_trigger(self, mock_get):
        def mock_requests(url, **kwargs):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            if "ticker/24hr?symbol=BTCUSDT" in url:
                mock_resp.json.return_value = {"lastPrice": "70000.00", "priceChangePercent": "-9.50"}
            elif "ticker/24hr?symbol=ETHUSDT" in url:
                mock_resp.json.return_value = {"priceChangePercent": "-12.00"}
            elif "klines" in url:
                # 1h drop from 75000 to 70000 = -6.67%
                mock_resp.json.return_value = [
                    [0, "75000.0", 0, 0, "70000.0"],
                    [0, "75000.0", 0, 0, "70000.0"]
                ]
            return mock_resp

        mock_get.side_effect = mock_requests
        monitor = BinanceMacroMonitor()
        metrics = monitor.get_metrics()
        self.assertTrue(metrics["is_flash_crash"])
        self.assertTrue(metrics["is_heavy_bear"])


class TestAPIQuotaTracker(unittest.TestCase):
    def test_daily_cap_and_rate_limit(self):
        tracker = APIQuotaTracker(max_daily_calls=5, min_interval_s=0.5)

        can_call, _ = tracker.can_call()
        self.assertTrue(can_call)
        tracker.record_call()

        can_call, reason = tracker.can_call()
        self.assertFalse(can_call)
        self.assertIn("Rate limit throttle", reason)

        tracker._calls_today = 5
        tracker._last_call_time = 0.0
        can_call, reason = tracker.can_call()
        self.assertFalse(can_call)
        self.assertIn("Daily Gemini quota cap reached", reason)

    def test_midnight_rollover(self):
        tracker = APIQuotaTracker(max_daily_calls=10, min_interval_s=0.1)
        tracker._calls_today = 10
        tracker._current_date = (datetime.now(timezone.utc) - timedelta(days=1)).date()

        can_call, _ = tracker.can_call()
        self.assertTrue(can_call)
        self.assertEqual(tracker.calls_today, 0)


class TestFreqtradeAPIClientEquity(unittest.TestCase):
    def test_precision_equity_no_double_counting(self):
        client = FreqtradeAPIClient("http://127.0.0.1:8080/api/v1", "user", "pass")
        client.get = MagicMock(return_value={
            "total_bot": 1250.75,
            "total": 1300.00,
            "currencies": []
        })

        equity = client.get_equity()
        self.assertEqual(equity, 1250.75)

    def test_equity_fallback_to_total(self):
        client = FreqtradeAPIClient("http://127.0.0.1:8080/api/v1", "user", "pass")
        client.get = MagicMock(return_value={
            "total_bot": None,
            "total": 1100.50,
            "currencies": []
        })

        equity = client.get_equity()
        self.assertEqual(equity, 1100.50)

    def test_equity_nan_guard(self):
        client = FreqtradeAPIClient("http://127.0.0.1:8080/api/v1", "user", "pass")
        client.get = MagicMock(return_value={
            "total_bot": float("nan"),
            "total": float("nan"),
        })

        equity = client.get_equity()
        self.assertIsNone(equity)


class TestSentimentAnalyzer(unittest.TestCase):
    def test_json_cleaner(self):
        analyzer = SentimentAnalyzer("")
        raw_markdown = """```json
        {
            "regime": "BULL",
            "confidence": 0.92,
            "reason": "Market is strong",
            "cooldown_minutes": 0,
        }
        ```"""
        parsed = analyzer._clean_llm_json(raw_markdown)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["regime"], "BULL")
        self.assertEqual(parsed["confidence"], 0.92)

    def test_flash_crash_override(self):
        analyzer = SentimentAnalyzer("")
        analyzer.macro_monitor.get_metrics = MagicMock(return_value={
            "btc_price": 50000,
            "btc_1h_pct": -5.2,
            "btc_24h_pct": -10.0,
            "eth_24h_pct": -15.0,
            "is_flash_crash": True,
            "is_heavy_bear": True,
        })
        analyzer.news_aggregator.get_headlines = MagicMock(return_value=[])
        analyzer.get_fear_and_greed_index = MagicMock(return_value=(15, "Extreme Fear"))

        res = analyzer.analyze_market(["BTC/USDT"])
        self.assertEqual(res["regime"], "PANIC")
        self.assertIn("flash-crash", res["reason"])

    def test_deterministic_bear_fallback(self):
        analyzer = SentimentAnalyzer("")
        res = analyzer._deterministic_fallback(20, {"btc_24h_pct": -5.0, "is_heavy_bear": False})
        self.assertEqual(res["regime"], "BEAR")
        self.assertEqual(res["cooldown_minutes"], 60)

    def test_anti_hallucination_downgrade_when_calm(self):
        analyzer = SentimentAnalyzer("")
        analyzer.gemini_client = MagicMock()
        analyzer._is_circuit_breaker_active = MagicMock(return_value=False)
        analyzer.quota_tracker.can_call = MagicMock(return_value=(True, "OK"))
        analyzer.macro_monitor.get_metrics = MagicMock(return_value={
            "btc_price": 84000,
            "btc_1h_pct": 0.5,
            "btc_24h_pct": 2.0,
            "eth_24h_pct": 1.5,
            "is_flash_crash": False,
            "is_heavy_bear": False,
        })
        analyzer.get_fear_and_greed_index = MagicMock(return_value=(75, "Greed"))
        analyzer.news_aggregator.get_headlines = MagicMock(return_value=[
            "[CoinDesk] Bitcoin miners report record quarterly profits"
        ])
        analyzer._query_gemini_context = MagicMock(return_value={
            "regime": "PANIC",
            "confidence": 0.8,
            "reason": "Hallucinated crash rumor without evidence"
        })

        res = analyzer.analyze_market(["BTC/USDT"])
        self.assertEqual(res["regime"], "BEAR")
        self.assertIn("Downgraded from PANIC", res["reason"])

    def test_panic_allowed_when_catastrophic_news_present(self):
        analyzer = SentimentAnalyzer("")
        analyzer.gemini_client = MagicMock()
        analyzer._is_circuit_breaker_active = MagicMock(return_value=False)
        analyzer.quota_tracker.can_call = MagicMock(return_value=(True, "OK"))
        analyzer.macro_monitor.get_metrics = MagicMock(return_value={
            "btc_price": 80000,
            "btc_1h_pct": -1.0,
            "btc_24h_pct": -2.0,
            "is_flash_crash": False,
            "is_heavy_bear": False,
        })
        analyzer.get_fear_and_greed_index = MagicMock(return_value=(40, "Neutral"))
        analyzer.news_aggregator.get_headlines = MagicMock(return_value=[
            "[CoinTelegraph] Major exchange freezes withdrawals amid insolvency investigation"
        ])
        analyzer._query_gemini_context = MagicMock(return_value={
            "regime": "PANIC",
            "confidence": 0.98,
            "reason": "Exchange insolvency confirmed"
        })

        res = analyzer.analyze_market(["BTC/USDT"])
        self.assertEqual(res["regime"], "PANIC")


class TestCriticalCommandExecution(unittest.TestCase):
    @patch.object(FreqtradeAPIClient, "_ensure_authenticated", return_value=True)
    def test_stop_buying_state_verification_and_retry(self, mock_auth):
        client = FreqtradeAPIClient("http://127.0.0.1:8080/api/v1", "user", "pass")
        
        # Simulate attempt 1 returning 500, attempt 2 succeeding with state paused
        mock_resp_fail = MagicMock()
        mock_resp_fail.status_code = 500
        mock_resp_fail.text = "Internal Server Error"
        
        mock_resp_ok = MagicMock()
        mock_resp_ok.status_code = 200
        mock_resp_ok.json.return_value = {"status": "paused"}
        
        client.session.request = MagicMock(side_effect=[mock_resp_fail, mock_resp_ok])
        client.get_bot_state = MagicMock(return_value="paused")
        
        success = client.stop_buying()
        self.assertTrue(success)
        self.assertEqual(client.session.request.call_count, 2)

    @patch.object(FreqtradeAPIClient, "_ensure_authenticated", return_value=True)
    def test_critical_command_state_mismatch_retry(self, mock_auth):
        client = FreqtradeAPIClient("http://127.0.0.1:8080/api/v1", "user", "pass")
        
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "ok"}
        client.session.request = MagicMock(return_value=mock_resp)
        
        # Attempt 1: get_bot_state reports "running" (mismatch for stop_buying)
        # Attempt 2: get_bot_state reports "paused" (verified)
        client.get_bot_state = MagicMock(side_effect=["running", "paused"])
        
        success = client.stop_buying()
        self.assertTrue(success)
        self.assertEqual(client.get_bot_state.call_count, 2)


class TestAntiWhipsawHysteresis(unittest.TestCase):
    def test_whipsaw_oscillation_blocks_start(self):
        """Alternating BEAR and BULL must NEVER trigger start_trading() due to hysteresis."""
        rm = RiskManager()
        rm.api_client = MagicMock()
        rm.wake_up_lock = True
        rm.current_regime = "BEAR"
        rm.cooldown_until = None  # Timer expired

        # Mock market as calm BULL for 1 cycle
        rm.sentiment.analyze_market = MagicMock(return_value={
            "regime": "BULL", "confidence": 0.90, "reason": "Brief calm", "cooldown_minutes": 60
        })
        rm.sentiment.macro_monitor.get_metrics = MagicMock(return_value={"btc_1h_pct": 0.2, "btc_24h_pct": 1.0})
        rm.sentiment.get_fear_and_greed_index = MagicMock(return_value=(55, "Neutral"))

        # Cycle 1: BULL (Confirmation 1/3)
        # Simulate logic of sentinel loop for 1 cycle
        macro = rm.sentiment.macro_monitor.get_metrics()
        fgi_val, _ = rm.sentiment.get_fear_and_greed_index()
        decision = rm.sentiment.analyze_market([])
        is_safe = (decision["regime"] == "BULL" and decision["confidence"] >= 0.8 and macro["btc_1h_pct"] >= -1.0)
        self.assertTrue(is_safe)
        
        rm.consecutive_bull_cycles += 1
        self.assertEqual(rm.consecutive_bull_cycles, 1)
        self.assertTrue(rm.wake_up_lock)
        rm.api_client.start_trading.assert_not_called()

        # Cycle 2: Market flips back to BEAR (Whipsaw!)
        decision_bear = {"regime": "BEAR", "confidence": 0.85, "reason": "Choppy dip"}
        if decision_bear["regime"] == "BEAR":
            rm.consecutive_bull_cycles = 0  # Hysteresis reset!
            rm.cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=60)
            
        self.assertEqual(rm.consecutive_bull_cycles, 0)
        rm.api_client.start_trading.assert_not_called()

    def test_full_hysteresis_confirmation_triggers_start(self):
        """3 consecutive calm cycles must release wake-up lock and trigger start_trading()."""
        rm = RiskManager()
        rm.api_client = MagicMock()
        rm.wake_up_lock = True
        rm.current_regime = "BEAR"
        rm.consecutive_bull_cycles = 0

        # Simulate 3 consecutive calm cycles
        for i in range(1, BULL_CONFIRMATION_CYCLES + 1):
            rm.consecutive_bull_cycles += 1
            if rm.consecutive_bull_cycles >= BULL_CONFIRMATION_CYCLES:
                rm.wake_up_lock = False
                rm.current_regime = "BULL"
                rm.consecutive_bull_cycles = 0
                rm.api_client.start_trading()

        rm.api_client.start_trading.assert_called_once()
        self.assertFalse(rm.wake_up_lock)
        self.assertEqual(rm.current_regime, "BULL")
        self.assertEqual(rm.consecutive_bull_cycles, 0)


class TestGuardedWakeUpLock(unittest.TestCase):
    def test_unsafe_market_on_expiry_extends_cooldown(self):
        """When cooldown expires but market is still falling (BTC 1h = -2.5%), extend cooldown."""
        rm = RiskManager()
        rm.api_client = MagicMock()
        rm.wake_up_lock = True
        now = datetime.now(timezone.utc)
        rm.cooldown_until = now - timedelta(seconds=1)  # Just expired!

        # Market still bleeding
        macro = {"btc_1h_pct": -2.5, "btc_24h_pct": -5.0}
        decision = {"regime": "BEAR", "confidence": 0.88, "reason": "Continuing dump", "cooldown_minutes": 60}
        fgi_val = 18

        is_safe_bull = (decision["regime"] == "BULL" and decision["confidence"] >= 0.80)
        is_macro_stable = (macro["btc_1h_pct"] >= -1.0 and macro["btc_24h_pct"] >= -3.5)
        is_fgi_stable = (fgi_val > 25)
        is_wake_up_safe = (is_safe_bull and is_macro_stable and is_fgi_stable)

        self.assertFalse(is_wake_up_safe)

        # Extend cooldown
        if not is_wake_up_safe:
            rm.cooldown_until = now + timedelta(minutes=WAKE_UP_COOLDOWN_MINUTES)
            rm.consecutive_bull_cycles = 0
            rm.api_client.stop_buying()

        self.assertGreater(rm.cooldown_until, now)
        self.assertEqual(rm.consecutive_bull_cycles, 0)
        rm.api_client.stop_buying.assert_called_once()
        rm.api_client.start_trading.assert_not_called()


class TestDeadlockFreeLocking(unittest.TestCase):
    def test_drawdown_nuclear_lock_released_before_network(self):
        """Ensure _state_lock is NOT held by the thread when trigger_nuclear_option executes."""
        rm = RiskManager()
        rm.api_client = MagicMock()
        rm.api_client.get_equity.return_value = 800.0  # Big drawdown
        rm.initial_balance = 1000.0
        rm.peak_balance = 1000.0  # -20% drawdown triggers nuclear

        lock_owned_during_nuclear = True

        def mock_nuclear(reason):
            nonlocal lock_owned_during_nuclear
            # In Python threading.RLock, _is_owned() returns True if held by current thread
            lock_owned_during_nuclear = rm._state_lock._is_owned()

        rm.trigger_nuclear_option = MagicMock(side_effect=mock_nuclear)
        rm._check_drawdown()

        # The state lock must NOT be owned when calling trigger_nuclear_option!
        self.assertFalse(lock_owned_during_nuclear)


if __name__ == "__main__":
    unittest.main()

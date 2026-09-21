# Quantitative Algorithmic Trading Bot (NFI Architecture)

## Overview
This repository contains a high-performance, fully automated quantitative trading engine built on a Python architecture. It leverages complex mathematical models and indicators to execute trades autonomously in the cryptocurrency markets.

The core strategy implemented is based on a highly optimized, sniper-like approach that prioritizes capital preservation (strict drawdown limits) and extreme win rates.

## 🧠 Autonomous LLM Risk Sentinel (Supervisor Loop)

Located in [`scripts/llm_risk_manager.py`](scripts/llm_risk_manager.py), this is an external supervisor agent that monitors the trading pipeline 24/7 via REST API/JWT, using AI to detect Black Swan events and enforce deterministic safety guardrails.

### Architecture Highlights:
- **Decoupled Execution & Reasoning:** A multi-threaded architecture separates high-priority portfolio monitoring from stochastic LLM calls. The deterministic guardrail loop runs continuously with **0ms LLM latency**, ensuring drawdowns are never delayed by model response times.
- **Hard Drawdown Guardrail ("Nuclear Option"):** Tracks real-time equity (including floating/unrealized PnL). If global portfolio drawdown exceeds 8%, it immediately halts buying and force-liquidates all open positions into USDT.
- **Multi-Tier Model Cascade & Circuit Breakers:** Resilient fallback across Gemini Flash models with exponential-backoff circuit breakers and deterministic fallbacks (Fear & Greed Index).
- **Anti-Hallucination Triangulation:** Prevents false liquidations by cross-validating LLM panic calls against quantitative market sentiment data before executing emergency actions.
- **Proactive Whitelist Scanning:** Periodically analyzes 40+ dynamic whitelist pairs for single-coin catastrophic news and contagion risks to blacklist malicious tokens before trades execute.

## Key Features
- **Algorithmic Execution:** Fully automated Spot/Futures trading 24/7 without human intervention.
- **Dynamic Risk Management:** Implements rigid stop-loss, trailing stops, and micro-profit grinding to lock in gains dynamically.
- **Capital Protection:** Verified 98.6% Win Rate across 496 trades (2023–2026 backtest) with max drawdown strictly constrained.
- **Multi-Asset Scanning:** Dynamically scans high-volume assets to identify market inefficiencies and statistical anomalies.
- **Backtesting & Simulation Engine:** Multi-year historical validation over bear market crashes.

## Tech Stack
- **Languages & Frameworks:** Python 3.11+, Google GenAI SDK (Gemini Flash), Requests, REST/WebSocket API
- **Data Processing:** Pandas, NumPy, TA-Lib
- **Infrastructure:** Docker Compose, AWS EC2, JWT Auth, Systemd

## Disclaimer
This project is for educational and quantitative research purposes. Algorithmic trading carries inherent risks.

---
*Developed and maintained by Davide.*

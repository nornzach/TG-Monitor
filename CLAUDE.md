# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

TG Monitor Platform — a local Telegram group/channel monitoring system. Collects messages from Telegram via Telethon, stores them in MySQL, runs AI-powered analysis (summaries, URL extraction), and serves a web dashboard via FastAPI. UI is in Chinese.

## Tech Stack

- Python 3.11, FastAPI, SQLAlchemy + PyMySQL (MySQL 5.7/8.0, database: `tg_monitor`)
- Telethon for Telegram API, opentele for Desktop session import
- Jinja2 + Chart.js for frontend
- OpenAI SDK pointed at DeepSeek API for AI summaries
- APScheduler for background collection
- jieba for Chinese keyword extraction

## Commands

```bash
# Setup
source .venv/bin/activate
pip install -r requirements.txt

# Database init
python -m app.cli init-db

# Telegram auth (one-time, creates data/telethon.session)
python -m app.cli manual-login --phone +8613xxxxxxxxx
python -m app.cli sync-dialogs

# Backfill a specific chat
python -m app.cli backfill --chat-id <telegram_id> --limit 1000

# Run web server (http://127.0.0.1:8098)
python main.py
```

No test suite or linter is configured.

## Architecture

**Entry points:**
- `main.py` — starts uvicorn, serves `app.web:app`
- `app/cli.py` — CLI for init-db, manual-login, sync-dialogs, backfill

**Core modules (`app/`):**
- `config.py` — Pydantic Settings from `.env`. All runtime config flows through the singleton `settings`.
- `db.py` — SQLAlchemy engine, `SessionLocal`, `Base`, `init_database()`, `session_scope()` context manager.
- `models.py` — ORM models: `MonitoredChat`, `TelegramUser`, `Message`, `MessageKeyword`, `SyncRun`, `AppSetting`, `AiSummary`, `AiUrl`.
- `telegram_client.py` — `TelegramSessionManager` singleton. Manages Telethon client lifecycle, manual login flow, desktop tdata import. Enforces `create_new` mode to avoid sharing Telegram Desktop's session.
- `collector.py` — `TelegramCollector` singleton. APScheduler-driven background collection: dialog sync, message backfill per active chat, live NewMessage handler (optional). Triggers AI summaries after backfill.
- `ai_service.py` — DeepSeek integration via OpenAI SDK. Summarizes message batches into structured JSON (summary text, categorized URLs, top senders). Results stored in `AiSummary` and `AiUrl` tables.
- `analysis.py` — `dashboard_metrics()` aggregation query for the web dashboard.
- `text_utils.py` — `normalize_text()` (strips URLs/mentions) and `extract_keywords()` (jieba-based with stopword filtering).
- `web.py` — FastAPI app with Jinja2 templates. Pages: `/` (dashboard), `/chats`, `/messages`, `/summaries`, `/urls`, `/settings`. Instance lock prevents duplicate processes.

**Data flow:**
1. Telegram messages arrive via Telethon (backfill polling or live handler)
2. `collector.persist_message()` normalizes text, extracts keywords, optionally downloads media, upserts into MySQL
3. After backfill, `_try_trigger_summary()` checks if enough new messages accumulated, then fires `ai_service.run_summary_for_chat()`
4. AI results populate `Ai_summaries` and `ai_urls` tables, displayed on `/summaries` and `/urls` pages

## Key Design Decisions

- **Independent Telegram session required.** Never share Telegram Desktop's session (`use_current` mode is force-disabled). The monitoring service must have its own authorization to avoid interfering with Desktop.
- **Desktop import locked to `create_new`.** The validator in `config.py` forces this; changing it will be overridden.
- **Background collection is off by default.** `TELEGRAM_BACKGROUND_COLLECTION_ENABLED=false` — must be explicitly enabled after manual login is confirmed working.
- **AI summaries use DeepSeek API** via OpenAI-compatible SDK at `api.deepseek.com`. API key stored in `app_settings` DB table, configured via `/settings` page.
- **Per-chat locking.** `collector` uses separate asyncio locks for backfill and persist operations per chat to prevent concurrent writes.

## Environment

Config via `.env` file (see `.env.example`). Key variables:
- `DATABASE_HOST/PORT/USER/PASSWORD/NAME` — MySQL connection
- `TELEGRAM_API_ID/API_HASH` — optional, falls back to opentele's bundled Desktop credentials
- `TELEGRAM_SESSION_PATH` — defaults to `./data/telethon.session`
- `TELEGRAM_SESSION_MODE` — `manual` (default) or `existing`
- `TELEGRAM_BACKGROUND_COLLECTION_ENABLED` — must be `true` for scheduled collection
- `SYNC_INTERVAL_MINUTES`, `SYNC_BATCH_SIZE`, `SYNC_LOOKBACK_MESSAGES` — collection tuning

## Conventions

- All web UI text and user-facing messages are in Chinese.
- SQLAlchemy models use `mapped_column` style (SQLAlchemy 2.0).
- Database indexes are managed both declaratively in models and imperatively in `db.ensure_runtime_indexes()` for indexes that need conditional creation.
- The `.env` file is updated in-place by `web._update_env_file()` with file locking — not a standard dotenv pattern.

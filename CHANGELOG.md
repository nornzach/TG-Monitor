# Changelog / 更新日志

All notable changes to this project will be documented in this file.

## [1.0.0] - 2026-05-27

### Added / 新增

**Core Features**
- Telegram message collection via Telethon (backfill + real-time listener)
- MySQL persistent storage with optimized indexes
- FastAPI web dashboard with responsive design

**AI Analysis**
- Multi-provider AI support: DeepSeek, OpenAI, Claude (Anthropic), custom OpenAI-compatible
- Automatic message summarization with configurable batch size
- URL extraction and classification (relay/seller/other)
- Manual AI trigger per chat

**Web Interface**
- Dashboard with interactive Chart.js charts (daily trend, hourly distribution)
- Group management with batch enable/disable, per-chat detail page
- Message browsing with filters (group, keyword, sender, media-only)
- AI summary management (view, re-run, delete)
- URL classification browser with category filtering
- System status page (sync tasks, AI jobs, error log)
- Settings page with all configurable options exposed

**Infrastructure**
- APScheduler background collection with configurable intervals
- Instance locking to prevent duplicate processes
- `.env` file hot-reload with file locking
- jieba-based Chinese keyword extraction
- Optional media file download
- CLI tools: `init-db`, `manual-login`, `sync-dialogs`, `backfill`

**Documentation**
- Bilingual README (Chinese + English)
- Deployment guide (local, systemd, Docker, Docker Compose)
- Contributing guide
- MIT License

### Fixed / 修复

- Decimal serialization error in dashboard API JSON response
- Race condition in URL upsert causing duplicate hash errors
- Deprecated FastAPI `on_event` replaced with lifespan context manager

---

## [0.1.0] - 2026-05-26

### Added
- Initial commit with basic project structure

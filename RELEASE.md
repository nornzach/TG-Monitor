# Release Notes / 发布说明

## v1.0.0 (2026-05-27)

### Highlights / 亮点

- **Multi-Provider AI**: DeepSeek, OpenAI, Claude, and custom endpoints
- **Complete Web UI**: 9 pages covering all functionality
- **Production Ready**: Docker support, systemd service, reverse proxy config

### What's New / 新功能

**AI Analysis**
- Multi-provider support with provider selector in settings
- Automatic URL classification (relay/seller/other)
- Manual AI trigger per chat
- Re-run and delete summaries

**Web Dashboard**
- Interactive charts (daily trend, hourly distribution)
- Per-chat detail page with stats and message browsing
- System status page with real-time monitoring
- Enhanced message filters (sender, media-only)

**Infrastructure**
- Optimized database indexes for fast queries
- Instance locking prevents duplicate processes
- Graceful error handling and timeout management

### Upgrade Guide / 升级指南

From any previous version:

```bash
git pull origin main
pip install -r requirements.txt
python -m app.cli init-db  # Safe to run again, creates missing tables
```

AI settings will auto-migrate from old `deepseek_api_key`/`deepseek_model` keys to new multi-provider format.

### Known Issues / 已知问题

- Telethon SQLite sessions may lock under heavy concurrent access
- No automated test suite yet

---

For full changelog, see [CHANGELOG.md](CHANGELOG.md).

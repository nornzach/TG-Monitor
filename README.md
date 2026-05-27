# TG Monitor Platform

[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.136-009688.svg)](https://fastapi.tiangolo.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**English** | [中文](#中文)

---

A local Telegram group/channel monitoring system with AI-powered analysis. Collects messages via Telethon, stores them in MySQL, runs multi-provider AI analysis (summaries, URL extraction, product/contact extraction), and serves a responsive web dashboard.

## Features

| Category | Feature |
|----------|---------|
| **Message Collection** | Auto/manual backfill of Telegram group/channel history, real-time listener |
| **AI Analysis** | Multi-provider AI (DeepSeek / OpenAI / Claude / custom) generates summaries, extracts URLs, products, and contacts |
| **Web Dashboard** | Responsive UI with interactive Chart.js charts, keyword clouds, activity rankings |
| **URL Classification** | Auto-categorizes URLs into relay / seller / other with reputation scoring |
| **Product Tracking** | Extracts product names, prices, seller contacts from messages |
| **Contact Extraction** | Extracts Telegram users, groups, emails, phone numbers |
| **Alert System** | Keyword/regex alert rules with web notifications |
| **Per-Chat Detail** | Stats, message browsing, manual sync, AI trigger per group |
| **System Health** | Real-time status page for sync tasks, AI jobs, error tracking |
| **Keyword Extraction** | jieba-based Chinese keyword analysis with configurable stopwords |
| **Media Download** | Optional auto-download of images, videos, and files |
| **i18n** | Chinese / English UI with language switcher |

## Quick Start

### Prerequisites

- Python 3.11+
- MySQL 5.7+ or 8.0+
- A Telegram account

### 1. Clone & Install

```bash
git clone https://github.com/your-username/tg-monitor-platform.git
cd tg-monitor-platform

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env` — at minimum set your MySQL credentials:

```bash
DATABASE_HOST=127.0.0.1
DATABASE_PORT=3306
DATABASE_USER=root
DATABASE_PASSWORD=your_password
DATABASE_NAME=tg_monitor
```

### 3. Initialize Database

```bash
# Option A: Auto-create tables via CLI
python -m app.cli init-db

# Option B: Import SQL manually
mysql -u root -p tg_monitor < docs/schema.sql
```

### 4. Telegram Login

First-time login requires phone verification:

```bash
python -m app.cli manual-login --phone +8613xxxxxxxxx
```

The system will send a verification code to your Telegram. Enter it via the web UI at `/settings` or re-run the command with `--code`.

### 5. Sync Groups

```bash
python -m app.cli sync-dialogs
```

This discovers all your Telegram groups/channels. Then enable monitoring for desired groups via the web UI at `/chats`.

### 6. Start Server

```bash
python main.py
```

Visit `http://127.0.0.1:8098`.

## Pages

| Page | Path | Description |
|------|------|-------------|
| Dashboard | `/` | Daily message trend, hourly distribution, top senders, keyword cloud, sync history |
| Groups | `/chats` | Manage monitored groups — batch enable/disable, backfill, per-chat detail |
| Messages | `/messages` | Browse all messages with filters (group, keyword, sender, media-only) |
| AI Summaries | `/summaries` | View AI-generated summaries, re-run or delete |
| URL Classification | `/urls` | Browse categorized URLs with domain stats and reputation |
| Products | `/products` | Extracted products with price, status, seller info |
| Contacts | `/contacts` | Extracted contacts grouped by type (TG user, email, phone, etc.) |
| Alerts | `/alerts` | Manage keyword/regex alert rules, view match history |
| System Status | `/status` | Sync tasks, AI jobs, error log, system metrics |
| Settings | `/settings` | Telegram login, collection config, AI provider, app settings |

## AI Provider Configuration

The system supports multiple AI providers. Configure via `/settings` page:

| Provider | API Type | Default Model |
|----------|----------|---------------|
| DeepSeek | OpenAI Compatible | `deepseek-chat` |
| OpenAI | OpenAI Compatible | `gpt-4o-mini` |
| Claude | Anthropic | `claude-sonnet-4-20250514` |
| Mimo AI | OpenAI Compatible | `mimo-v2.5-pro` |
| Custom | OpenAI Compatible | User-defined |

Select provider, enter API Key, adjust Base URL and model as needed.

## Configuration Reference

### Application

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_NAME` | TG Monitor Platform | App name shown in UI |
| `APP_HOST` | 127.0.0.1 | Listen address (`0.0.0.0` for LAN access) |
| `APP_PORT` | 8098 | Listen port |

### Database

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_HOST` | 127.0.0.1 | MySQL host |
| `DATABASE_PORT` | 3306 | MySQL port |
| `DATABASE_USER` | root | Database user |
| `DATABASE_PASSWORD` | — | Database password |
| `DATABASE_NAME` | tg_monitor | Database name |

### Telegram

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_API_ID` | — | API ID from https://my.telegram.org (optional) |
| `TELEGRAM_API_HASH` | — | API Hash (optional) |
| `TELEGRAM_SESSION_PATH` | ./data/telethon.session | Session file path |
| `TELEGRAM_SESSION_MODE` | manual | `manual` (recommended) or `existing` |

### Collection

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BACKGROUND_COLLECTION_ENABLED` | false | Enable scheduled backfill |
| `TELEGRAM_LIVE_LISTENER_ENABLED` | false | Real-time message listener |
| `TELEGRAM_DOWNLOAD_MEDIA_ENABLED` | false | Download media files |
| `TELEGRAM_FETCH_USER_ABOUT_ENABLED` | false | Fetch user bio on first encounter |
| `SYNC_INTERVAL_MINUTES` | 5 | Backfill interval (minutes) |
| `SYNC_BATCH_SIZE` | 200 | Incremental sync batch size |
| `SYNC_LOOKBACK_MESSAGES` | 1000 | First-time backfill message count |

### AI Summary

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_SUMMARY_BATCH_SIZE` | 100 | Messages before triggering AI summary |
| `AI_SUMMARY_RUNNING_TIMEOUT_MINUTES` | 30 | Running task timeout |

> AI API Key and model are stored in the database, configured via `/settings` page.

## Architecture

```
tg-monitor-platform/
├── main.py                  # Entry: uvicorn startup
├── app/
│   ├── web.py               # FastAPI routes (20+ endpoints)
│   ├── config.py             # Pydantic Settings from .env
│   ├── db.py                 # SQLAlchemy engine + session
│   ├── models.py             # 13 ORM models
│   ├── cli.py                # CLI commands (init-db, manual-login, sync-dialogs, backfill)
│   ├── collector.py          # APScheduler background collector
│   ├── telegram_client.py    # Telethon session management
│   ├── ai_service.py         # Multi-provider AI analysis
│   ├── analysis.py           # Dashboard aggregation queries
│   ├── text_utils.py         # Text processing + keyword extraction
│   ├── i18n/                 # Translation files (zh.json, en.json)
│   ├── templates/            # Jinja2 templates (12 pages)
│   └── static/               # CSS styles
├── docs/
│   ├── schema.sql            # Database schema (13 tables)
│   ├── DEPLOY.md             # Deployment guide
│   ├── CONTRIBUTING.md       # Contributing guide
│   └── README.md             # Documentation index
├── .env.example              # Environment template
├── requirements.txt          # Python dependencies
├── CHANGELOG.md              # Version history
└── LICENSE                   # MIT License
```

### Data Flow

```
Telegram ──→ Telethon ──→ collector.persist_message() ──→ MySQL
                                    │
                             normalize_text() + extract_keywords()
                                    │
                             _try_trigger_summary()
                                    │
                              ai_service.run_summary_for_chat()
                                    │
                         ┌──────────┼──────────┐
                         ▼          ▼          ▼
                    AiSummary    AiUrl    AiProduct / AiContact
```

## Database Schema

13 tables — see `docs/schema.sql` for full DDL.

| Table | Description |
|-------|-------------|
| `app_settings` | Key-value app configuration |
| `monitored_chats` | Monitored Telegram groups/channels |
| `telegram_users` | Telegram user profiles |
| `messages` | Collected messages with metadata |
| `message_keywords` | Extracted keywords per message |
| `sync_runs` | Sync task history |
| `ai_summaries` | AI-generated summaries |
| `ai_urls` | Extracted and classified URLs |
| `ai_url_appearances` | URL appearance tracking across chats |
| `ai_products` | Extracted products with pricing |
| `ai_contacts` | Extracted contacts by type |
| `alert_rules` | Keyword/regex alert rules |
| `alert_matches` | Alert match records |

## Security

- **Independent session**: Uses its own Telegram authorization, never shares Telegram Desktop's session
- **Local deployment**: All data stored in local MySQL, no third-party uploads
- **API key storage**: AI keys stored in database, managed via web UI
- **No hardcoded secrets**: All credentials via `.env` or database settings

## Deployment

See [docs/DEPLOY.md](docs/DEPLOY.md) for detailed guides:

- Local development
- systemd service
- Docker / Docker Compose
- Nginx reverse proxy

## CLI Commands

```bash
# Initialize database
python -m app.cli init-db

# Telegram login (first-time)
python -m app.cli manual-login --phone +8613xxxxxxxxx

# Discover groups/channels
python -m app.cli sync-dialogs

# Backfill a specific group
python -m app.cli backfill --chat-id <telegram_id> --limit 1000
```

## Contributing

See [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md).

## License

[MIT License](LICENSE)

---

# 中文

[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.136-009688.svg)](https://fastapi.tiangolo.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

本地部署的 Telegram 群组/频道消息监控系统，支持 AI 智能分析。通过 Telethon 采集消息，存入 MySQL，调用多种 AI 服务商进行分析（摘要、URL 提取、商品/联系方式提取），并提供响应式 Web 仪表盘。

## 功能特性

| 分类 | 功能 |
|------|------|
| **消息采集** | 自动/手动回填 Telegram 群组历史，实时监听新消息 |
| **AI 分析** | 多服务商 AI（DeepSeek / OpenAI / Claude / 自定义）生成摘要、提取 URL、商品、联系方式 |
| **Web 仪表盘** | 响应式 UI，Chart.js 交互图表、关键词云、活跃度排行 |
| **URL 分类** | 自动分类为中转站/号商/其他，支持信誉评分 |
| **商品追踪** | 从消息中提取商品名称、价格、卖家联系方式 |
| **联系方式提取** | 提取 TG 用户、群组、邮箱、手机号等联系方式 |
| **告警系统** | 关键词/正则告警规则，Web 通知 |
| **群组详情** | 每个群组的统计、消息浏览、手动同步、AI 触发 |
| **系统状态** | 实时查看同步任务、AI 任务、错误日志 |
| **关键词提取** | 基于 jieba 的中文关键词分析，可配置停用词 |
| **媒体下载** | 可选自动下载图片、视频、文件 |
| **多语言** | 中文/英文界面，侧边栏一键切换 |

## 快速开始

### 环境要求

- Python 3.11+
- MySQL 5.7+ 或 8.0+
- 一个 Telegram 账号

### 1. 克隆 & 安装

```bash
git clone https://github.com/your-username/tg-monitor-platform.git
cd tg-monitor-platform

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. 配置

```bash
cp .env.example .env
```

编辑 `.env`，至少设置 MySQL 连接信息：

```bash
DATABASE_HOST=127.0.0.1
DATABASE_PORT=3306
DATABASE_USER=root
DATABASE_PASSWORD=你的密码
DATABASE_NAME=tg_monitor
```

### 3. 初始化数据库

```bash
# 方式一：通过 CLI 自动建表
python -m app.cli init-db

# 方式二：手动导入 SQL
mysql -u root -p tg_monitor < docs/schema.sql
```

### 4. Telegram 登录

首次登录需要手机验证码：

```bash
python -m app.cli manual-login --phone +8613xxxxxxxxx
```

系统会向你的 Telegram 发送验证码。通过 Web 界面 `/settings` 页面输入验证码完成登录，或重新运行命令并带上 `--code` 参数。

### 5. 同步群组

```bash
python -m app.cli sync-dialogs
```

此命令会发现你所有的 Telegram 群组/频道。然后通过 Web 界面 `/chats` 页面启用需要监控的群组。

### 6. 启动服务

```bash
python main.py
```

访问 `http://127.0.0.1:8098`。

## 页面说明

| 页面 | 路径 | 说明 |
|------|------|------|
| 仪表盘 | `/` | 每日消息趋势、小时分布、活跃用户、关键词云、同步历史 |
| 群组管理 | `/chats` | 管理监控群组 — 批量启用/停用、回填、群组详情 |
| 消息浏览 | `/messages` | 按群组/关键词/发送者筛选，支持仅媒体过滤 |
| AI 总结 | `/summaries` | 查看 AI 生成的摘要，支持重新生成和删除 |
| URL 分类 | `/urls` | 浏览分类 URL，域名统计、信誉评分 |
| 商品信息 | `/products` | 提取的商品，含价格、状态、卖家信息 |
| 联系方式 | `/contacts` | 按类型分组的联系方式（TG 用户、邮箱、手机等） |
| 告警系统 | `/alerts` | 管理关键词/正则告警规则，查看匹配记录 |
| 系统状态 | `/status` | 同步任务、AI 任务、错误日志、系统指标 |
| 连接设置 | `/settings` | Telegram 登录、采集配置、AI 服务商、应用设置 |

## AI 服务商配置

系统支持多种 AI 服务商，通过 `/settings` 页面配置：

| 服务商 | API 类型 | 默认模型 |
|--------|----------|----------|
| DeepSeek | OpenAI 兼容 | `deepseek-chat` |
| OpenAI | OpenAI 兼容 | `gpt-4o-mini` |
| Claude | Anthropic | `claude-sonnet-4-20250514` |
| Mimo AI | OpenAI 兼容 | `mimo-v2.5-pro` |
| 自定义 | OpenAI 兼容 | 用户自定义 |

选择服务商，输入 API Key，按需调整 Base URL 和模型。

## 配置说明

### 应用

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `APP_NAME` | TG Monitor Platform | 界面显示的应用名称 |
| `APP_HOST` | 127.0.0.1 | 监听地址（设为 `0.0.0.0` 可局域网访问） |
| `APP_PORT` | 8098 | 监听端口 |

### 数据库

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DATABASE_HOST` | 127.0.0.1 | MySQL 主机 |
| `DATABASE_PORT` | 3306 | MySQL 端口 |
| `DATABASE_USER` | root | 数据库用户 |
| `DATABASE_PASSWORD` | — | 数据库密码 |
| `DATABASE_NAME` | tg_monitor | 数据库名 |

### Telegram

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `TELEGRAM_API_ID` | — | API ID（可选，从 https://my.telegram.org 获取） |
| `TELEGRAM_API_HASH` | — | API Hash（可选） |
| `TELEGRAM_SESSION_PATH` | ./data/telethon.session | 会话文件路径 |
| `TELEGRAM_SESSION_MODE` | manual | `manual`（推荐）或 `existing` |

### 采集

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `TELEGRAM_BACKGROUND_COLLECTION_ENABLED` | false | 启用定时回填 |
| `TELEGRAM_LIVE_LISTENER_ENABLED` | false | 实时消息监听 |
| `TELEGRAM_DOWNLOAD_MEDIA_ENABLED` | false | 下载媒体文件 |
| `TELEGRAM_FETCH_USER_ABOUT_ENABLED` | false | 首次遇到用户时获取简介 |
| `SYNC_INTERVAL_MINUTES` | 5 | 回填间隔（分钟） |
| `SYNC_BATCH_SIZE` | 200 | 增量同步批次大小 |
| `SYNC_LOOKBACK_MESSAGES` | 1000 | 首次回填消息数量 |

### AI 摘要

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `AI_SUMMARY_BATCH_SIZE` | 100 | 触发 AI 摘要的消息数阈值 |
| `AI_SUMMARY_RUNNING_TIMEOUT_MINUTES` | 30 | 运行中超时时间 |

> AI API Key 和模型存储在数据库中，通过 `/settings` 页面配置。

## 项目架构

```
tg-monitor-platform/
├── main.py                  # 入口：uvicorn 启动
├── app/
│   ├── web.py               # FastAPI 路由（20+ 端点）
│   ├── config.py             # Pydantic Settings，从 .env 加载
│   ├── db.py                 # SQLAlchemy 引擎 + 会话管理
│   ├── models.py             # 13 个 ORM 模型
│   ├── cli.py                # CLI 命令（init-db, manual-login, sync-dialogs, backfill）
│   ├── collector.py          # APScheduler 后台采集器
│   ├── telegram_client.py    # Telethon 会话管理
│   ├── ai_service.py         # 多服务商 AI 分析
│   ├── analysis.py           # 仪表盘聚合查询
│   ├── text_utils.py         # 文本处理 + 关键词提取
│   ├── i18n/                 # 翻译文件（zh.json, en.json）
│   ├── templates/            # Jinja2 模板（12 个页面）
│   └── static/               # CSS 样式
├── docs/
│   ├── schema.sql            # 数据库建表 SQL（13 张表）
│   ├── DEPLOY.md             # 部署指南
│   ├── CONTRIBUTING.md       # 贡献指南
│   └── README.md             # 文档目录
├── .env.example              # 环境变量模板
├── requirements.txt          # Python 依赖
├── CHANGELOG.md              # 版本历史
└── LICENSE                   # MIT 许可证
```

### 数据流

```
Telegram ──→ Telethon ──→ collector.persist_message() ──→ MySQL
                                    │
                             normalize_text() + extract_keywords()
                                    │
                             _try_trigger_summary()
                                    │
                              ai_service.run_summary_for_chat()
                                    │
                         ┌──────────┼──────────┐
                         ▼          ▼          ▼
                    AiSummary    AiUrl    AiProduct / AiContact
```

## 数据库表结构

共 13 张表，完整建表语句见 `docs/schema.sql`。

| 表名 | 说明 |
|------|------|
| `app_settings` | 应用配置键值表 |
| `monitored_chats` | 监控的群组/频道 |
| `telegram_users` | Telegram 用户信息 |
| `messages` | 采集的消息及元数据 |
| `message_keywords` | 消息提取的关键词 |
| `sync_runs` | 同步任务记录 |
| `ai_summaries` | AI 生成的摘要 |
| `ai_urls` | 提取并分类的 URL |
| `ai_url_appearances` | URL 跨群组出现记录 |
| `ai_products` | 提取的商品及价格 |
| `ai_contacts` | 按类型提取的联系方式 |
| `alert_rules` | 关键词/正则告警规则 |
| `alert_matches` | 告警匹配记录 |

## 安全说明

- **独立会话**：使用独立的 Telegram 授权，不共享 Telegram Desktop 的会话
- **本地部署**：所有数据存储在本地 MySQL，不上传至第三方
- **密钥管理**：AI 密钥存储在数据库中，通过 Web 界面管理
- **无硬编码密钥**：所有凭据通过 `.env` 或数据库配置

## 部署方式

详见 [docs/DEPLOY.md](docs/DEPLOY.md)：

- 本地开发
- systemd 服务
- Docker / Docker Compose
- Nginx 反向代理

## CLI 命令

```bash
# 初始化数据库
python -m app.cli init-db

# Telegram 登录（首次）
python -m app.cli manual-login --phone +8613xxxxxxxxx

# 发现群组/频道
python -m app.cli sync-dialogs

# 回填指定群组
python -m app.cli backfill --chat-id <telegram_id> --limit 1000
```

## 贡献

见 [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md)。

## 许可证

[MIT License](LICENSE)

# TG Monitor Platform

一个可在本地持续监控 Telegram 群组/频道消息的完整工程，目标是：
- 读取你当前可访问的 Telegram 群/频道数据
- 存储到本地 MySQL 新库（仅使用 `tg_monitor`）
- 做基础分析（活跃群、活跃用户、关键词、消息趋势）
- 提供 Web 页面查看和管理

## 技术栈
- Python 3.11
- FastAPI
- SQLAlchemy + PyMySQL
- Telethon
- OpenTele（优先尝试导入 Telegram Desktop 当前登录会话）
- Jinja2 + Chart.js
- MySQL

## 项目路径
默认已放到：
- `/Users/zach/GolandProjects/tg-monitor-platform`

## 数据库约束
本项目只会创建和使用一个新库：
- `tg_monitor`

不会写入你现有的 `infron_kms` 库。

## 已写好的默认配置
`.env` 已按你提供的本地 MySQL 参数写好，默认：
- host: `127.0.0.1`
- port: `3306`
- user: `root`
- database: `tg_monitor`

Telegram Desktop tdata 默认路径：
- `/Users/zach/Library/Application Support/Telegram Desktop/tdata`

## 安装依赖
```bash
cd /Users/zach/GolandProjects/tg-monitor-platform
source .venv/bin/activate
pip install -r requirements.txt
```

## 初始化数据库
```bash
cd /Users/zach/GolandProjects/tg-monitor-platform
source .venv/bin/activate
python -m app.cli init-db
```

## 方式一：优先尝试导入当前 Telegram Desktop 已登录会话
```bash
cd /Users/zach/GolandProjects/tg-monitor-platform
source .venv/bin/activate
python -m app.cli import-desktop-session
```

如果成功，会生成本地 Telethon session 文件：
- `./data/telethon.session`

然后同步你账号下可见的群/频道列表：
```bash
python -m app.cli sync-dialogs
```

## 方式二：桌面会话导入失败时，改用手动登录
只有在你自己提供 `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` 时可用。

`.env` 里补充：
```env
TELEGRAM_SESSION_MODE=manual
TELEGRAM_API_ID=你的_api_id
TELEGRAM_API_HASH=你的_api_hash
```

手动登录：
```bash
python -m app.cli manual-login --phone +8613xxxxxxxxx
```

## 启动 Web 页面
```bash
cd /Users/zach/GolandProjects/tg-monitor-platform
source .venv/bin/activate
python main.py
```

打开：
- `http://127.0.0.1:8098`

## 页面说明
- `/` 仪表盘：消息趋势、活跃群、活跃用户、关键词
- `/chats` 群组/频道：同步对话列表、启用/停用监控、回填历史消息
- `/messages` 消息浏览：按群或关键词筛选最近消息
- `/settings` 连接设置：查看当前 session 状态，尝试连接 Telegram

## 推荐使用流程
1. `python -m app.cli init-db`
2. `python -m app.cli import-desktop-session`
3. `python -m app.cli sync-dialogs`
4. 打开 `/chats`，把需要监控的群/频道点“启用”
5. 对重要群先点一次“回填”
6. 运行 `python main.py` 持续监听新消息

## 注意
1. 只能读取你当前账号本来就有权限访问的群/频道。
2. 如果 Telegram Desktop 的本地数据格式和 OpenTele 兼容性不一致，桌面会话导入可能失败；这时走手动登录备用方案。
3. 关键词分析目前是基础版，适合做数据库丰富和运营观察；如果你后面要做分类、实体识别、情绪分析，可以继续往上扩展。

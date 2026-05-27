# Deployment Guide / 部署指南

## Prerequisites / 前置要求

- Python 3.11+
- MySQL 5.7+ or 8.0+
- A Telegram account for monitoring
- (Optional) AI API key for summaries

---

## Local Development / 本地开发

```bash
# Clone
git clone https://github.com/your-username/tg-monitor-platform.git
cd tg-monitor-platform

# Virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your MySQL credentials

# Initialize database (auto-create tables)
python -m app.cli init-db

# Or import schema manually:
# mysql -u root -p tg_monitor < docs/schema.sql

# Telegram login (one-time)
python -m app.cli manual-login --phone +8613xxxxxxxxx

# Sync groups
python -m app.cli sync-dialogs

# Start server
python main.py
```

---

## Production Deployment / 生产部署

### Option 1: systemd Service

Create `/etc/systemd/system/tg-monitor.service`:

```ini
[Unit]
Description=TG Monitor Platform
After=network.target mysql.service

[Service]
Type=simple
User=tgmonitor
WorkingDirectory=/opt/tg-monitor-platform
Environment=PATH=/opt/tg-monitor-platform/.venv/bin
ExecStart=/opt/tg-monitor-platform/.venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable tg-monitor
sudo systemctl start tg-monitor
```

### Option 2: Docker

```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p data

EXPOSE 8098
CMD ["python", "main.py"]
```

```bash
docker build -t tg-monitor .
docker run -d \
  --name tg-monitor \
  -p 8098:8098 \
  -v ./data:/app/data \
  -v ./.env:/app/.env \
  --restart unless-stopped \
  tg-monitor
```

### Option 3: Docker Compose

```yaml
version: '3.8'

services:
  mysql:
    image: mysql:8.0
    environment:
      MYSQL_ROOT_PASSWORD: your_password
      MYSQL_DATABASE: tg_monitor
    volumes:
      - mysql_data:/var/lib/mysql
    ports:
      - "3306:3306"

  tg-monitor:
    build: .
    ports:
      - "8098:8098"
    volumes:
      - ./data:/app/data
      - ./.env:/app/.env
    depends_on:
      - mysql
    restart: unless-stopped

volumes:
  mysql_data:
```

---

## Reverse Proxy / 反向代理

### Nginx

```nginx
server {
    listen 80;
    server_name monitor.example.com;

    location / {
        proxy_pass http://127.0.0.1:8098;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /media/ {
        alias /opt/tg-monitor-platform/data/media/;
        expires 30d;
    }
}
```

---

## Environment Variables / 环境变量

See `.env.example` for all available options. Key production settings:

```bash
# Bind to all interfaces for LAN access
APP_HOST=0.0.0.0
APP_PORT=8098

# MySQL connection
DATABASE_HOST=127.0.0.1
DATABASE_PORT=3306
DATABASE_USER=tgmonitor
DATABASE_PASSWORD=secure_password
DATABASE_NAME=tg_monitor

# Enable background collection after confirming login works
TELEGRAM_BACKGROUND_COLLECTION_ENABLED=true
```

---

## Backup / 备份

```bash
# Database backup
mysqldump -u root -p tg_monitor > backup_$(date +%Y%m%d).sql

# Session file backup
cp data/telethon.session backup/

# Restore
mysql -u root -p tg_monitor < backup_20260527.sql
```

---

## Troubleshooting / 常见问题

### Port already in use / 端口被占用

```bash
lsof -ti :8098 | xargs kill -9
rm -f data/tg-monitor-platform.lock
```

### Telegram session locked / 会话被锁定

```bash
rm -f data/telethon.session-journal
# Restart the service
```

### Database connection refused / 数据库连接失败

Check MySQL is running and credentials in `.env` are correct:

```bash
mysql -u root -p -e "SELECT 1"
```

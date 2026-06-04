# TG Monitor External Query API

This document describes the read-only GET APIs for external systems that need paginated access to URL classification data, chat messages, products, and contacts.

## General

- Base URL: `http://127.0.0.1:8098`
- Method: `GET`
- Response format: `application/json`
- Authentication: not enabled by default. Expose these endpoints only on a trusted network or behind an authenticated reverse proxy.
- Datetime format: `YYYY-MM-DD HH:MM:SS.ffffff`

All list endpoints return the same envelope:

```json
{
  "pagination": {
    "page": 1,
    "page_size": 50,
    "total": 1380,
    "total_pages": 28,
    "has_prev": false,
    "has_next": true
  },
  "items": []
}
```

Common pagination parameters:

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `page` | integer | `1` | Page number, starting from 1 |
| `page_size` | integer | `50` | Items per page, from `1` to `200` |

## URL Classification Data

Endpoint:

```http
GET /api/urls
```

Filters:

| Parameter | Type | Description |
| --- | --- | --- |
| `category` | string | Original coarse category: `relay`, `seller`, or `other` |
| `ai_category_id` | integer | AI dynamic category ID |
| `ai_category_slug` | string | AI dynamic category slug, such as `telegram_group` or `cloud_drive` |
| `classification_status` | string | Classification status: `pending`, `running`, `classified`, or `failed` |
| `keyword` | string | Fuzzy search by URL or domain |

Example:

```bash
curl "http://127.0.0.1:8098/api/urls?page=1&page_size=50&ai_category_slug=telegram_group"
```

Item fields:

```json
{
  "id": 293,
  "url": "https://t.me/example",
  "domain": "t.me",
  "category": "other",
  "appearance_count": 3,
  "chat_ids_seen": {"10": "2026-05-27T08:49:48"},
  "reputation_score": 0.5,
  "classification_status": "classified",
  "primary_category": {
    "id": 1,
    "slug": "telegram_group",
    "name": "Telegram Group/Channel",
    "description": "t.me group, channel, and invitation links",
    "source": "ai"
  },
  "classification_run_id": 7,
  "classified_at": "2026-05-27 22:25:16",
  "classification_error": null,
  "first_seen_at": "2026-05-26 04:25:50",
  "last_seen_at": "2026-05-27 08:49:48"
}
```

## Chat Messages

Endpoint:

```http
GET /api/messages
```

Filters:

| Parameter | Type | Description |
| --- | --- | --- |
| `chat_id` | integer | Internal chat ID |
| `keyword` | string | Fuzzy search by message text |
| `media_only` | boolean | Return only messages with media. Use `true` or `false` |
| `sender` | string | Exact sender match by username or first name |

Example:

```bash
curl "http://127.0.0.1:8098/api/messages?page=1&page_size=100&keyword=Claude"
```

Item fields:

```json
{
  "id": 22571,
  "chat": {
    "id": 10,
    "telegram_id": -1001234567890,
    "title": "AGI Wholesale Center",
    "username": null,
    "chat_type": "channel",
    "is_active": true
  },
  "sender": {
    "id": 501,
    "telegram_id": 123456,
    "username": "demo",
    "first_name": "Demo",
    "last_name": null,
    "is_bot": false
  },
  "telegram_message_id": 1234,
  "message_date": "2026-05-27 12:16:58",
  "edit_date": null,
  "raw_text": "Original message text",
  "normalized_text": "Original message text",
  "reply_to_msg_id": null,
  "views": 10,
  "forwards": 0,
  "has_media": false,
  "media_type": null,
  "meta_json": {},
  "created_at": "2026-05-27 12:19:12"
}
```

## Products

Endpoint:

```http
GET /api/products
```

Filters:

| Parameter | Type | Description |
| --- | --- | --- |
| `status` | string | Product status: `available`, `sold`, or `reserved` |
| `chat_id` | integer | Internal chat ID |
| `keyword` | string | Fuzzy search by product name or seller contact |

Example:

```bash
curl "http://127.0.0.1:8098/api/products?page=1&page_size=50&status=available"
```

Item fields:

```json
{
  "id": 12,
  "chat": {
    "id": 10,
    "telegram_id": -1001234567890,
    "title": "AGI Wholesale Center",
    "username": null,
    "chat_type": "channel",
    "is_active": true
  },
  "summary_id": 54,
  "product_name": "Claude Pro account",
  "price_amount": 120.0,
  "price_currency": "CNY",
  "seller_contact": "@seller",
  "status": "available",
  "first_seen_at": "2026-05-27 08:49:48",
  "last_seen_at": "2026-05-27 08:49:48"
}
```

## Contacts

Endpoint:

```http
GET /api/contacts
```

Filters:

| Parameter | Type | Description |
| --- | --- | --- |
| `contact_type` | string | Contact type: `tg_user`, `tg_group`, `email`, `phone`, or `other` |
| `chat_id` | integer | Internal chat ID |
| `keyword` | string | Fuzzy search by contact value or context |

Example:

```bash
curl "http://127.0.0.1:8098/api/contacts?page=1&page_size=50&contact_type=tg_user"
```

Item fields:

```json
{
  "id": 88,
  "chat": {
    "id": 10,
    "telegram_id": -1001234567890,
    "title": "AGI Wholesale Center",
    "username": null,
    "chat_type": "channel",
    "is_active": true
  },
  "summary_id": 54,
  "contact_type": "tg_user",
  "contact_value": "@seller",
  "context": null,
  "first_seen_at": "2026-05-27 08:49:48",
  "last_seen_at": "2026-05-27 08:49:48"
}
```

## Dashboard Metrics

Endpoint:

```http
GET /api/dashboard
```

No parameters. Returns an aggregate snapshot of the entire system.

Example:

```bash
curl "http://127.0.0.1:8098/api/dashboard"
```

Response fields:

```json
{
  "total_chats": 12,
  "active_chats": 8,
  "total_messages": 45230,
  "messages_24h": 312,
  "messages_7d": 2180,
  "total_users": 1520,
  "media_count": 8300,
  "total_summaries": 86,
  "success_summaries": 80,
  "total_urls": 392,
  "total_products": 45,
  "total_contacts": 210,
  "total_alert_rules": 3,
  "unread_alerts": 5,
  "daily_rows": [
    {"day": "2026-05-20", "count": 310},
    {"day": "2026-05-21", "count": 285}
  ],
  "hourly_dist": [
    {"hour": 0, "count": 42},
    {"hour": 1, "count": 18}
  ],
  "top_chats": [
    {"title": "AGI Wholesale Center", "chat_id": 10, "message_count": 8200}
  ],
  "top_senders": [
    {"sender": "demo", "message_count": 320}
  ],
  "top_keywords": [
    {"keyword": "Claude", "weight": 128.5}
  ]
}
```

| Field | Type | Description |
| --- | --- | --- |
| `total_chats` | integer | Total monitored chats |
| `active_chats` | integer | Currently active (enabled) chats |
| `total_messages` | integer | Total messages stored |
| `messages_24h` | integer | Messages received in the last 24 hours |
| `messages_7d` | integer | Messages received in the last 7 days |
| `total_users` | integer | Unique Telegram users seen |
| `media_count` | integer | Messages containing media |
| `total_summaries` | integer | Total AI summary runs |
| `success_summaries` | integer | Successful AI summaries |
| `total_urls` | integer | Extracted URLs |
| `total_products` | integer | Extracted products |
| `total_contacts` | integer | Extracted contacts |
| `total_alert_rules` | integer | Configured alert rules |
| `unread_alerts` | integer | Unread alert matches |
| `daily_rows` | array | Message count per day (last 14 days) |
| `hourly_dist` | array | Message count per hour of day (last 7 days) |
| `top_chats` | array | Top 10 chats by message count |
| `top_senders` | array | Top 10 senders by message count |
| `top_keywords` | array | Top keywords by weight |

## Unread Alerts

Endpoint:

```http
GET /api/alerts/unread
```

No parameters. Returns the count of unread alert matches and the 10 most recent unread items.

Example:

```bash
curl "http://127.0.0.1:8098/api/alerts/unread"
```

Response fields:

```json
{
  "count": 5,
  "items": [
    {
      "id": 12,
      "rule_id": 3,
      "chat_id": 10,
      "matched_text": "Found keyword match: Claude in message...",
      "matched_at": "2026-05-27T12:16:58"
    }
  ]
}
```

| Field | Type | Description |
| --- | --- | --- |
| `count` | integer | Total unread alert matches |
| `items` | array | Up to 10 most recent unread matches |
| `items[].id` | integer | Match ID |
| `items[].rule_id` | integer | Alert rule ID that triggered |
| `items[].chat_id` | integer | Internal chat ID where the match occurred |
| `items[].matched_text` | string | First 100 characters of the matched text |
| `items[].matched_at` | string | ISO datetime of the match |

## URL Statistics

Endpoint:

```http
GET /api/url-stats
```

No parameters. Returns aggregate URL analytics: domain frequency, category breakdown, cross-chat URLs, reputation distribution, and daily trend.

Example:

```bash
curl "http://127.0.0.1:8098/api/url-stats"
```

Response fields:

```json
{
  "domains": [
    {"domain": "t.me", "count": 120},
    {"domain": "github.com", "count": 45}
  ],
  "categories": [
    {"category": "relay", "count": 80},
    {"category": "seller", "count": 60},
    {"category": "other", "count": 252}
  ],
  "cross_chat_urls": [
    {
      "id": 293,
      "url": "https://t.me/example",
      "domain": "t.me",
      "title": null,
      "category": "other",
      "appearance_count": 5,
      "chat_count": 3,
      "reputation_score": 0.5,
      "first_seen_at": "2026-05-26T04:25:50"
    }
  ],
  "reputation": {
    "total": 392,
    "high": 120,
    "medium": 90,
    "low": 42,
    "unscored": 140
  },
  "trend": [
    {"day": "2026-05-01", "count": 12},
    {"day": "2026-05-02", "count": 8}
  ]
}
```

| Field | Type | Description |
| --- | --- | --- |
| `domains` | array | Top domains by URL count (default limit 20) |
| `categories` | array | URL count per coarse category (`relay`, `seller`, `other`) |
| `cross_chat_urls` | array | URLs appearing in 2+ chats, sorted by appearance count |
| `reputation` | object | Reputation score distribution: `total`, `high` (>=0.7), `medium` (0.3-0.7), `low` (<0.3), `unscored` |
| `trend` | array | Daily new URL count for the last 30 days |

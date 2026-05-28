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

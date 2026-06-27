"""Read-only tools for the AI data-query chat agent."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from .db import engine
from .models import MonitoredChat

logger = logging.getLogger(__name__)

# Simple in-memory cache for expensive database stats (refreshed every 60s).
_db_stats_cache: dict[str, Any] | None = None
_db_stats_cached_at: datetime | None = None
_DB_STATS_TTL_SECONDS = 60

# Dangerous SQL keywords that must never appear in generated SQL.
FORBIDDEN_KEYWORDS = {
    'insert', 'update', 'delete', 'drop', 'alter', 'truncate',
    'create', 'grant', 'revoke', 'lock', 'exec', 'execute',
    'merge', 'replace', 'load', 'call', 'pragma', 'into',
    'outfile', 'dumpfile', 'load_file', 'handler', 'prepare',
}

# Maximum rows returned from a single query to keep context size reasonable.
MAX_RESULT_ROWS = 100
# Maximum query execution time in milliseconds (MySQL MAX_EXECUTION_TIME).
MAX_EXECUTION_TIME_MS = 30_000


@dataclass(frozen=True)
class SchemaColumn:
    name: str
    dtype: str
    nullable: bool
    comment: str


@dataclass(frozen=True)
class SchemaTable:
    name: str
    columns: list[SchemaColumn]
    indexes: list[str]
    notes: str


# Hand-curated schema description used in the LLM prompt.
# Keep in sync with app/models.py.
_SCHEMA_TABLES: list[SchemaTable] = [
    SchemaTable(
        name='monitored_chats',
        columns=[
            SchemaColumn('id', 'INT PK', False, '内部群ID，其他表用 chat_id 引用'),
            SchemaColumn('telegram_id', 'BIGINT', False, 'Telegram 原始 chat id'),
            SchemaColumn('title', 'VARCHAR(255)', False, '群/频道标题'),
            SchemaColumn('username', 'VARCHAR(255)', True, 't.me 用户名'),
            SchemaColumn('chat_type', 'VARCHAR(50)', False, "'group' | 'channel' | 'supergroup'"),
            SchemaColumn('is_active', 'BOOLEAN', False, '是否正在监控'),
            SchemaColumn('last_message_at', 'DATETIME', True, '最后一条消息时间'),
        ],
        indexes=['id', 'telegram_id', 'title'],
        notes='监控对象列表，所有消息都归属到某个 chat_id。',
    ),
    SchemaTable(
        name='messages',
        columns=[
            SchemaColumn('id', 'INT PK', False, '内部消息ID'),
            SchemaColumn('chat_id', 'INT FK', False, '归属群ID'),
            SchemaColumn('sender_user_id', 'INT FK', True, '发送者 telegram_users.id'),
            SchemaColumn('telegram_message_id', 'INT', False, 'TG 原始消息ID'),
            SchemaColumn('message_date', 'DATETIME', False, '消息发送时间（UTC）'),
            SchemaColumn('raw_text', 'TEXT', True, '原始文本'),
            SchemaColumn('normalized_text', 'TEXT', True, '清洗后文本，适合 LIKE'),
            SchemaColumn('has_media', 'BOOLEAN', False, '是否含媒体'),
            SchemaColumn('media_type', 'VARCHAR(50)', True, 'media 类型'),
            SchemaColumn('views', 'INT', True, '浏览量（频道）'),
            SchemaColumn('forwards', 'INT', True, '转发数'),
            SchemaColumn('meta_json', 'JSON', True, '元数据'),
        ],
        indexes=['chat_id,message_date', 'chat_id,id', 'message_date'],
        notes='核心消息表。统计消息数、关键词、活跃度都从这里查。',
    ),
    SchemaTable(
        name='telegram_users',
        columns=[
            SchemaColumn('id', 'INT PK', False, '内部用户ID'),
            SchemaColumn('telegram_id', 'BIGINT', False, 'TG 用户ID'),
            SchemaColumn('username', 'VARCHAR(255)', True, '@用户名'),
            SchemaColumn('first_name', 'VARCHAR(255)', True, '名'),
            SchemaColumn('last_name', 'VARCHAR(255)', True, '姓'),
            SchemaColumn('about', 'TEXT', True, '个人简介'),
        ],
        indexes=['id', 'telegram_id', 'username'],
        notes='用户资料。发消息的人可 JOIN 这个表查用户名。',
    ),
    SchemaTable(
        name='message_keywords',
        columns=[
            SchemaColumn('id', 'INT PK', False, ''),
            SchemaColumn('message_id', 'INT FK', False, 'messages.id'),
            SchemaColumn('keyword', 'VARCHAR(100)', False, 'jieba 分词后的关键词'),
            SchemaColumn('weight', 'INT', False, '权重'),
        ],
        indexes=['keyword'],
        notes='消息关键词，统计高频词用这个表。',
    ),
    SchemaTable(
        name='ai_summaries',
        columns=[
            SchemaColumn('id', 'INT PK', False, ''),
            SchemaColumn('chat_id', 'INT FK', False, ''),
            SchemaColumn('message_count', 'INT', False, '本次摘要包含消息数'),
            SchemaColumn('summary_text', 'TEXT', True, 'AI 生成的中文摘要'),
            SchemaColumn('extracted_urls', 'JSON', True, '提取的 URL、商品、联系人等'),
            SchemaColumn('status', 'VARCHAR(30)', False, "'success' | 'failed' | 'running'"),
            SchemaColumn('triggered_at', 'DATETIME', False, '开始时间'),
            SchemaColumn('completed_at', 'DATETIME', True, '完成时间'),
        ],
        indexes=['chat_id,status'],
        notes='群聊 AI 摘要。',
    ),
    SchemaTable(
        name='ai_urls',
        columns=[
            SchemaColumn('id', 'INT PK', False, ''),
            SchemaColumn('url', 'TEXT', False, '完整 URL'),
            SchemaColumn('url_hash', 'VARCHAR(64)', False, '去重哈希'),
            SchemaColumn('category', 'VARCHAR(20)', False, "'relay' | 'seller' | 'other'"),
            SchemaColumn('domain', 'VARCHAR(255)', True, '域名'),
            SchemaColumn('appearance_count', 'INT', False, '出现次数'),
            SchemaColumn('primary_category_id', 'INT FK', True, '细分类别'),
            SchemaColumn('classification_status', 'VARCHAR(20)', False, "'classified' | 'pending' | 'failed'"),
            SchemaColumn('first_seen_at', 'DATETIME', False, ''),
            SchemaColumn('last_seen_at', 'DATETIME', False, ''),
        ],
        indexes=['url_hash', 'domain', 'category'],
        notes='URL 库。',
    ),
    SchemaTable(
        name='ai_url_categories',
        columns=[
            SchemaColumn('id', 'INT PK', False, ''),
            SchemaColumn('slug', 'VARCHAR(80)', False, '分类标识'),
            SchemaColumn('name', 'VARCHAR(100)', False, '分类名'),
            SchemaColumn('description', 'TEXT', True, '描述'),
        ],
        indexes=['slug'],
        notes='URL 细分类别。',
    ),
    SchemaTable(
        name='ai_products',
        columns=[
            SchemaColumn('id', 'INT PK', False, ''),
            SchemaColumn('chat_id', 'INT FK', False, ''),
            SchemaColumn('product_name', 'VARCHAR(255)', False, '商品名'),
            SchemaColumn('price_amount', 'FLOAT', True, '价格'),
            SchemaColumn('price_currency', 'VARCHAR(20)', False, '货币'),
            SchemaColumn('seller_contact', 'VARCHAR(255)', True, '卖家联系'),
            SchemaColumn('status', 'VARCHAR(20)', False, "'available' | 'sold' | 'reserved'"),
            SchemaColumn('first_seen_at', 'DATETIME', False, ''),
            SchemaColumn('last_seen_at', 'DATETIME', False, ''),
        ],
        indexes=['chat_id,product_name'],
        notes='AI 提取的商品。',
    ),
    SchemaTable(
        name='product_price_history',
        columns=[
            SchemaColumn('id', 'INT PK', False, ''),
            SchemaColumn('product_id', 'INT FK', False, ''),
            SchemaColumn('price_amount', 'FLOAT', True, ''),
            SchemaColumn('price_currency', 'VARCHAR(20)', False, ''),
            SchemaColumn('seller_contact', 'VARCHAR(255)', True, ''),
            SchemaColumn('recorded_at', 'DATETIME', False, ''),
        ],
        indexes=['product_id', 'recorded_at'],
        notes='商品价格历史。',
    ),
    SchemaTable(
        name='ai_contacts',
        columns=[
            SchemaColumn('id', 'INT PK', False, ''),
            SchemaColumn('chat_id', 'INT FK', False, ''),
            SchemaColumn('contact_type', 'VARCHAR(30)', False, "'tg_user' | 'tg_group' | 'email' | 'phone' | 'other'"),
            SchemaColumn('contact_value', 'VARCHAR(255)', False, ''),
            SchemaColumn('context', 'TEXT', True, ''),
            SchemaColumn('first_seen_at', 'DATETIME', False, ''),
            SchemaColumn('last_seen_at', 'DATETIME', False, ''),
        ],
        indexes=['chat_id,contact_type,contact_value'],
        notes='AI 提取的联系方式。',
    ),
    SchemaTable(
        name='ai_key_leads',
        columns=[
            SchemaColumn('id', 'INT PK', False, ''),
            SchemaColumn('message_id', 'INT FK', False, ''),
            SchemaColumn('chat_id', 'INT FK', False, ''),
            SchemaColumn('sender_user_id', 'INT FK', True, ''),
            SchemaColumn('lead_type', 'VARCHAR(30)', False, "'api_key' | 'free_credit_account'"),
            SchemaColumn('provider', 'VARCHAR(60)', True, 'openai/anthropic/google/xai/groq/openrouter/other'),
            SchemaColumn('product_name', 'VARCHAR(255)', True, ''),
            SchemaColumn('offer_text', 'TEXT', True, ''),
            SchemaColumn('price_amount', 'FLOAT', True, ''),
            SchemaColumn('price_currency', 'VARCHAR(20)', True, ''),
            SchemaColumn('seller_contact', 'VARCHAR(255)', True, ''),
            SchemaColumn('seller_username', 'VARCHAR(255)', True, ''),
            SchemaColumn('confidence', 'FLOAT', True, ''),
            SchemaColumn('reason', 'TEXT', True, ''),
            SchemaColumn('source_text', 'TEXT', True, ''),
            SchemaColumn('first_seen_at', 'DATETIME', False, ''),
            SchemaColumn('last_seen_at', 'DATETIME', False, ''),
        ],
        indexes=['provider,lead_type', 'chat_id,last_seen_at'],
        notes='Key 商线索。查 free credits / API key 供给用这个表。',
    ),
    SchemaTable(
        name='market_intelligence_items',
        columns=[
            SchemaColumn('id', 'INT PK', False, ''),
            SchemaColumn('summary_id', 'INT FK', False, ''),
            SchemaColumn('chat_id', 'INT FK', False, ''),
            SchemaColumn('item_type', 'VARCHAR(30)', False, "'market'|'risk'|'price'|'legal'|'hotspot'|'gossip'|'industry'|'key_people'|'timeline'|'signal_types'"),
            SchemaColumn('content', 'TEXT', False, ''),
            SchemaColumn('confidence', 'FLOAT', True, ''),
            SchemaColumn('created_at', 'DATETIME', False, ''),
        ],
        indexes=['chat_id', 'item_type', 'created_at'],
        notes='结构化市场情报。',
    ),
    SchemaTable(
        name='daily_chat_stats',
        columns=[
            SchemaColumn('id', 'INT PK', False, ''),
            SchemaColumn('chat_id', 'INT FK', False, ''),
            SchemaColumn('date', 'DATETIME', False, '日期（通常只有年月日）'),
            SchemaColumn('message_count', 'INT', False, ''),
            SchemaColumn('unique_senders', 'INT', False, ''),
            SchemaColumn('media_count', 'INT', False, ''),
            SchemaColumn('url_count', 'INT', False, ''),
            SchemaColumn('new_user_count', 'INT', False, ''),
            SchemaColumn('top_keywords_json', 'JSON', True, ''),
            SchemaColumn('avg_message_length', 'FLOAT', True, ''),
        ],
        indexes=['chat_id', 'date'],
        notes='按天聚合的群统计。',
    ),
    SchemaTable(
        name='user_daily_stats',
        columns=[
            SchemaColumn('id', 'INT PK', False, ''),
            SchemaColumn('user_id', 'INT FK', False, ''),
            SchemaColumn('date', 'DATETIME', False, ''),
            SchemaColumn('message_count', 'INT', False, ''),
            SchemaColumn('word_count', 'INT', False, ''),
            SchemaColumn('media_count', 'INT', False, ''),
            SchemaColumn('active_hours_json', 'JSON', True, ''),
            SchemaColumn('top_chats_json', 'JSON', True, ''),
            SchemaColumn('reputation_score', 'FLOAT', True, ''),
        ],
        indexes=['user_id', 'date'],
        notes='用户按天统计。',
    ),
    SchemaTable(
        name='alert_rules',
        columns=[
            SchemaColumn('id', 'INT PK', False, ''),
            SchemaColumn('name', 'VARCHAR(100)', False, ''),
            SchemaColumn('pattern', 'TEXT', False, ''),
            SchemaColumn('pattern_type', 'VARCHAR(20)', False, "'keyword' | 'regex'"),
            SchemaColumn('is_active', 'BOOLEAN', False, ''),
        ],
        indexes=[],
        notes='告警规则。',
    ),
    SchemaTable(
        name='alert_matches',
        columns=[
            SchemaColumn('id', 'INT PK', False, ''),
            SchemaColumn('rule_id', 'INT FK', False, ''),
            SchemaColumn('message_id', 'INT FK', False, ''),
            SchemaColumn('chat_id', 'INT FK', False, ''),
            SchemaColumn('matched_text', 'TEXT', True, ''),
            SchemaColumn('matched_at', 'DATETIME', False, ''),
            SchemaColumn('is_read', 'BOOLEAN', False, ''),
        ],
        indexes=['rule_id,matched_at'],
        notes='告警命中记录。',
    ),
    SchemaTable(
        name='system_events',
        columns=[
            SchemaColumn('id', 'INT PK', False, ''),
            SchemaColumn('event_type', 'VARCHAR(50)', False, ''),
            SchemaColumn('severity', 'VARCHAR(20)', False, "'info'|'warning'|'error'|..."),
            SchemaColumn('chat_id', 'INT FK', True, ''),
            SchemaColumn('message_id', 'INT FK', True, ''),
            SchemaColumn('title', 'VARCHAR(255)', False, ''),
            SchemaColumn('detail', 'TEXT', True, ''),
            SchemaColumn('metric_value', 'FLOAT', True, ''),
            SchemaColumn('is_read', 'BOOLEAN', False, ''),
            SchemaColumn('created_at', 'DATETIME', False, ''),
        ],
        indexes=['event_type', 'severity', 'created_at'],
        notes='系统事件。',
    ),
]


def build_schema_description() -> str:
    """Return a compact schema description for LLM prompts."""
    lines: list[str] = []
    for table in _SCHEMA_TABLES:
        lines.append(f"\n### {table.name}")
        lines.append(table.notes)
        lines.append("Columns:")
        for col in table.columns:
            null_str = 'NULL' if col.nullable else 'NOT NULL'
            lines.append(f"  - {col.name}: {col.dtype} {null_str} — {col.comment}")
        if table.indexes:
            lines.append(f"Indexes: {', '.join(table.indexes)}")
    return '\n'.join(lines)


def is_safe_sql(sql: str) -> bool:
    """Check that SQL is read-only and does not contain forbidden keywords."""
    sql_lower = sql.strip().lower()
    if not sql_lower.startswith('select'):
        return False
    # Remove string literals before keyword scanning to avoid false positives.
    no_strings = re.sub(r"'[^']*'", "''", sql_lower)
    no_strings = re.sub(r'"[^"]*"', '""', no_strings)
    return not any(kw in no_strings for kw in FORBIDDEN_KEYWORDS)


def _serialize_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat(sep=' ')
    return value


def run_sql(db: Session, sql: str, max_rows: int = MAX_RESULT_ROWS) -> dict[str, Any]:
    """Execute a read-only SQL query and return results as JSON-serializable dict."""
    sql = sql.strip()
    if not is_safe_sql(sql):
        return {
            'ok': False,
            'error': 'SQL 安全检查失败：只能使用 SELECT 查询，禁止 DDL/DML。',
            'sql': sql,
            'rows': [],
        }
    try:
        # Guard against runaway queries from the LLM (MySQL only).
        try:
            db.execute(text(f'SET SESSION MAX_EXECUTION_TIME={MAX_EXECUTION_TIME_MS}'))
        except Exception:
            # Ignore on non-MySQL engines (e.g. SQLite used in tests).
            pass
        rows = db.execute(text(sql)).mappings().all()
        serialized = [
            {k: _serialize_value(v) for k, v in dict(row).items()}
            for row in rows[:max_rows]
        ]
        return {
            'ok': True,
            'row_count': len(serialized),
            'truncated': len(rows) > max_rows,
            'sql': sql,
            'rows': serialized,
        }
    except Exception as exc:
        logger.warning('AI chat SQL execution failed: %s', exc)
        return {
            'ok': False,
            'error': str(exc),
            'sql': sql,
            'rows': [],
        }


def get_chat_list(db: Session, keyword: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Return list of monitored chats for the LLM to map titles to IDs.

    If keyword is provided, only chats whose title or username contains it are returned.
    """
    query = db.query(MonitoredChat)
    if keyword:
        pattern = f'%{keyword}%'
        query = query.filter(
            (MonitoredChat.title.like(pattern)) | (MonitoredChat.username.like(pattern))
        )
    chats = (
        query.order_by(MonitoredChat.title.asc())
        .limit(min(max(limit, 1), 100))
        .all()
    )
    return [
        {
            'id': c.id,
            'title': c.title,
            'username': c.username,
            'chat_type': c.chat_type,
            'is_active': c.is_active,
        }
        for c in chats
    ]


def get_database_stats(db: Session) -> dict[str, Any]:
    """Return high-level row counts to help the LLM understand data volume."""
    global _db_stats_cache, _db_stats_cached_at
    now = datetime.utcnow()
    if _db_stats_cache and _db_stats_cached_at and (now - _db_stats_cached_at).total_seconds() < _DB_STATS_TTL_SECONDS:
        return dict(_db_stats_cache)

    try:
        inspector_obj = inspect(engine)
        tables = inspector_obj.get_table_names()
        counts: dict[str, int] = {}
        for table_name in tables:
            try:
                count = db.execute(text(f'SELECT COUNT(*) FROM `{table_name}`')).scalar_one()
                counts[table_name] = int(count or 0)
            except Exception:
                counts[table_name] = -1
        result = {'ok': True, 'counts': counts}
        _db_stats_cache = dict(result)
        _db_stats_cached_at = now
        return result
    except Exception as exc:
        return {'ok': False, 'error': str(exc)}


# Tool call result serialization helper.
def tool_result_to_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    try:
        text = json.dumps(result, ensure_ascii=False, default=str, indent=2)
        # Make escaped newlines/tabs readable in the LLM context while keeping it as text.
        return text.replace('\\n', '\n').replace('\\t', '\t')
    except Exception:
        return str(result)

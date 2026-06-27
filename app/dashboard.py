"""Dashboard data endpoints with sectioned, cacheable queries."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import func, desc, text
from sqlalchemy.orm import Session

from .config import settings
from .models import (
    AiContact,
    AiProduct,
    AiSummary,
    AiUrl,
    AlertMatch,
    AlertRule,
    KeywordSummary,
    Message,
    MessageKeyword,
    MonitoredChat,
    SyncRun,
    TelegramUser,
)

logger = logging.getLogger(__name__)

# Cache TTL for expensive dashboard sections (seconds).
_DASHBOARD_CACHE_TTL = 60


def _truncate(text: str | None, max_len: int = 160) -> str:
    if not text:
        return ''
    text = str(text).replace('\n', ' ').strip()
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + '...'


class _TimedCache:
    """Simple thread-safe TTL cache keyed by function name."""

    def __init__(self, ttl_seconds: float):
        self._ttl = ttl_seconds
        self._data: dict[str, tuple[Any, float]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        now = time.time()
        with self._lock:
            value, expires = self._data.get(key, (None, 0))
            if now < expires:
                return value
            self._data.pop(key, None)
        return None

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = (value, time.time() + self._ttl)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


dashboard_cache = _TimedCache(_DASHBOARD_CACHE_TTL)


def _cached_section(name: str, fn: Callable[[Session], Any], db: Session) -> Any:
    """Return cached section data or compute and cache it."""
    cached = dashboard_cache.get(name)
    if cached is not None:
        return cached
    value = fn(db)
    dashboard_cache.set(name, value)
    return value


def _table_approximate_counts(db: Session, table_names: list[str]) -> dict[str, int]:
    """Fetch approximate row counts from INFORMATION_SCHEMA (InnoDB)."""
    try:
        db_name = db.execute(text('SELECT DATABASE()')).scalar_one()
        rows = db.execute(
            text('''
                SELECT table_name, table_rows
                FROM information_schema.tables
                WHERE table_schema = :db_name AND table_name IN :table_names
            '''),
            {'db_name': db_name, 'table_names': tuple(table_names)},
        ).all()
        return {row.table_name.lower(): int(row.table_rows or 0) for row in rows}
    except Exception as exc:
        logger.warning('Failed to fetch approximate counts: %s', exc)
        return {}


def _approx_count(db: Session, table_name: str, fallback_query) -> int:
    """Use approximate count if available, otherwise run fallback COUNT(*)."""
    counts = _table_approximate_counts(db, [table_name])
    count = counts.get(table_name.lower())
    if count is not None:
        return count
    return fallback_query.scalar() or 0


def dashboard_overview(db: Session) -> dict[str, Any]:
    """Lightweight overview card: totals, active counts, 24h/7d activity."""
    now = datetime.utcnow()
    last_24h = now - timedelta(hours=24)
    last_7d = now - timedelta(days=7)

    # Approximate counts for large tables; exact for small ones.
    total_messages = _approx_count(
        db, 'messages',
        db.query(func.count(Message.id))
    )
    total_users = _approx_count(
        db, 'telegram_users',
        db.query(func.count(TelegramUser.id))
    )

    # Exact counts for small or operational tables.
    total_chats = db.query(func.count(MonitoredChat.id)).scalar() or 0
    active_chats = db.query(func.count(MonitoredChat.id)).filter(MonitoredChat.is_active.is_(True)).scalar() or 0
    messages_24h = db.query(func.count(Message.id)).filter(Message.message_date >= last_24h).scalar() or 0
    messages_7d = db.query(func.count(Message.id)).filter(Message.message_date >= last_7d).scalar() or 0
    media_count = db.query(func.count(Message.id)).filter(Message.has_media.is_(True)).scalar() or 0

    return {
        'total_chats': total_chats,
        'active_chats': active_chats,
        'total_messages': total_messages,
        'messages_24h': messages_24h,
        'messages_7d': messages_7d,
        'total_users': total_users,
        'media_count': media_count,
    }


def dashboard_activity(db: Session) -> dict[str, Any]:
    """Activity card: daily trend and hourly distribution."""
    now = datetime.utcnow()
    last_7d = now - timedelta(days=7)

    daily_rows = (
        db.query(func.date(Message.message_date), func.count(Message.id))
        .group_by(func.date(Message.message_date))
        .order_by(func.date(Message.message_date).desc())
        .limit(14)
        .all()
    )
    daily_rows.reverse()

    hourly_dist = (
        db.query(func.hour(Message.message_date), func.count(Message.id))
        .filter(Message.message_date >= last_7d)
        .group_by(func.hour(Message.message_date))
        .order_by(func.hour(Message.message_date))
        .all()
    )

    return {
        'daily_rows': [{'day': str(day), 'count': count} for day, count in daily_rows],
        'hourly_dist': [{'hour': int(h), 'count': int(c)} for h, c in hourly_dist],
    }


def dashboard_top_chats(db: Session, limit: int = 10) -> dict[str, Any]:
    """Top chats by message count."""
    rows = (
        db.query(MonitoredChat.title, MonitoredChat.id, func.count(Message.id).label('message_count'))
        .join(Message, Message.chat_id == MonitoredChat.id)
        .group_by(MonitoredChat.id, MonitoredChat.title)
        .order_by(desc('message_count'))
        .limit(limit)
        .all()
    )
    return {
        'top_chats': [{'title': title, 'chat_id': cid, 'message_count': count} for title, cid, count in rows],
    }


def dashboard_top_senders(db: Session, limit: int = 10) -> dict[str, Any]:
    """Top message senders."""
    rows = (
        db.query(
            func.coalesce(TelegramUser.username, TelegramUser.first_name, TelegramUser.last_name, TelegramUser.telegram_id).label('sender'),
            func.count(Message.id).label('message_count'),
        )
        .join(Message, Message.sender_user_id == TelegramUser.id)
        .group_by(TelegramUser.id, TelegramUser.username, TelegramUser.first_name, TelegramUser.last_name, TelegramUser.telegram_id)
        .order_by(desc('message_count'))
        .limit(limit)
        .all()
    )
    return {
        'top_senders': [{'sender': str(sender), 'message_count': count} for sender, count in rows],
    }


def dashboard_top_keywords(db: Session, limit: int | None = None) -> dict[str, Any]:
    """Top weighted keywords.

    Uses the pre-aggregated keyword_summary table for speed; falls back to a
    direct (slower) query if the summary table is empty.
    """
    if limit is None:
        limit = settings.analysis_top_keywords

    summary_count = db.query(func.count(KeywordSummary.keyword)).scalar() or 0
    if summary_count > 0:
        rows = (
            db.query(KeywordSummary.keyword, KeywordSummary.total_weight.label('weight'))
            .order_by(desc('weight'))
            .limit(limit)
            .all()
        )
    else:
        # Fallback when summary table hasn't been backfilled yet.
        rows = (
            db.query(MessageKeyword.keyword, func.sum(MessageKeyword.weight).label('weight'))
            .group_by(MessageKeyword.keyword)
            .order_by(desc('weight'))
            .limit(limit)
            .all()
        )
    return {
        'top_keywords': [{'keyword': keyword, 'weight': float(weight) if weight else 0} for keyword, weight in rows],
    }


def dashboard_ai_stats(db: Session) -> dict[str, Any]:
    """AI-generated content stats."""
    total_summaries = db.query(func.count(AiSummary.id)).scalar() or 0
    success_summaries = db.query(func.count(AiSummary.id)).filter(AiSummary.status == 'success').scalar() or 0
    total_urls = db.query(func.count(AiUrl.id)).scalar() or 0
    total_products = db.query(func.count(AiProduct.id)).scalar() or 0
    total_contacts = db.query(func.count(AiContact.id)).scalar() or 0
    total_alert_rules = db.query(func.count(AlertRule.id)).scalar() or 0
    unread_alerts = db.query(func.count(AlertMatch.id)).filter(AlertMatch.is_read.is_(False)).scalar() or 0

    return {
        'total_summaries': total_summaries,
        'success_summaries': success_summaries,
        'total_urls': total_urls,
        'total_products': total_products,
        'total_contacts': total_contacts,
        'total_alert_rules': total_alert_rules,
        'unread_alerts': unread_alerts,
    }


def dashboard_overview_cached(db: Session) -> dict[str, Any]:
    return _cached_section('overview', dashboard_overview, db)


def dashboard_activity_cached(db: Session) -> dict[str, Any]:
    return _cached_section('activity', dashboard_activity, db)


def dashboard_top_chats_cached(db: Session, limit: int = 10) -> dict[str, Any]:
    return _cached_section(f'top_chats:{limit}', lambda d: dashboard_top_chats(d, limit), db)


def dashboard_top_senders_cached(db: Session, limit: int = 10) -> dict[str, Any]:
    return _cached_section(f'top_senders:{limit}', lambda d: dashboard_top_senders(d, limit), db)


def dashboard_top_keywords_cached(db: Session, limit: int | None = None) -> dict[str, Any]:
    return _cached_section(f'top_keywords:{limit}', lambda d: dashboard_top_keywords(d, limit), db)


def dashboard_ai_stats_cached(db: Session) -> dict[str, Any]:
    return _cached_section('ai_stats', dashboard_ai_stats, db)


def dashboard_sync_status_cached(db: Session) -> dict[str, Any]:
    return _cached_section('sync_status', dashboard_sync_status, db)


def refresh_keyword_summary(db: Session) -> int:
    """Rebuild the keyword_summary materialized table. Returns row count."""
    from .models import KeywordSummary
    db.execute(text('TRUNCATE TABLE keyword_summary'))
    db.execute(text('''
        INSERT INTO keyword_summary (keyword, total_weight, updated_at)
        SELECT keyword, SUM(weight), NOW()
        FROM message_keywords
        GROUP BY keyword
    '''))
    db.commit()
    return db.query(func.count(KeywordSummary.keyword)).scalar() or 0


def dashboard_sync_status(db: Session) -> dict[str, Any]:
    """Sync / system status card."""
    now = datetime.utcnow()
    last_24h = now - timedelta(hours=24)

    running_syncs = db.query(func.count(SyncRun.id)).filter(SyncRun.status == 'running').scalar() or 0
    failed_syncs_24h = db.query(func.count(SyncRun.id)).filter(
        SyncRun.status == 'failed', SyncRun.started_at >= last_24h
    ).scalar() or 0
    success_syncs_24h = db.query(func.count(SyncRun.id)).filter(
        SyncRun.status == 'success', SyncRun.started_at >= last_24h
    ).scalar() or 0

    running_summaries = db.query(func.count(AiSummary.id)).filter(AiSummary.status == 'running').scalar() or 0
    failed_summaries = db.query(func.count(AiSummary.id)).filter(AiSummary.status == 'failed').scalar() or 0

    recent_runs = db.query(SyncRun).order_by(SyncRun.id.desc()).limit(10).all()

    recent_errors = db.query(SyncRun).filter(
        SyncRun.status == 'failed'
    ).order_by(SyncRun.id.desc()).limit(5).all()

    last_success = db.query(SyncRun).filter(
        SyncRun.status == 'success'
    ).order_by(SyncRun.id.desc()).first()

    return {
        'running_syncs': running_syncs,
        'failed_syncs_24h': failed_syncs_24h,
        'success_syncs_24h': success_syncs_24h,
        'running_summaries': running_summaries,
        'failed_summaries': failed_summaries,
        'recent_runs': [
            {
                'id': r.id,
                'run_type': r.run_type,
                'status': r.status,
                'message': _truncate(r.message, 160),
                'started_at': r.started_at.isoformat() if r.started_at else None,
            }
            for r in recent_runs
        ],
        'recent_errors': [
            {'id': r.id, 'message': _truncate(r.message, 160), 'started_at': r.started_at.isoformat() if r.started_at else None}
            for r in recent_errors
        ],
        'last_success_at': last_success.finished_at.isoformat() if last_success and last_success.finished_at else None,
    }

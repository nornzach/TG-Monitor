from datetime import datetime, timedelta
from sqlalchemy import func, desc, case
from sqlalchemy.orm import Session

from .models import Message, MonitoredChat, TelegramUser, MessageKeyword, AiSummary, SyncRun, AiUrl, AiUrlAppearance, AiProduct, AiContact, AlertRule, AlertMatch
from .config import settings


def dashboard_metrics(db: Session) -> dict:
    now = datetime.utcnow()
    last_24h = now - timedelta(hours=24)
    last_7d = now - timedelta(days=7)

    total_chats = db.query(func.count(MonitoredChat.id)).scalar() or 0
    active_chats = db.query(func.count(MonitoredChat.id)).filter(MonitoredChat.is_active.is_(True)).scalar() or 0
    total_messages = db.query(func.count(Message.id)).scalar() or 0
    messages_24h = db.query(func.count(Message.id)).filter(Message.message_date >= last_24h).scalar() or 0
    messages_7d = db.query(func.count(Message.id)).filter(Message.message_date >= last_7d).scalar() or 0
    total_users = db.query(func.count(TelegramUser.id)).scalar() or 0
    media_count = db.query(func.count(Message.id)).filter(Message.has_media.is_(True)).scalar() or 0

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

    top_chats = (
        db.query(MonitoredChat.title, MonitoredChat.id, func.count(Message.id).label('message_count'))
        .join(Message, Message.chat_id == MonitoredChat.id)
        .group_by(MonitoredChat.id, MonitoredChat.title)
        .order_by(desc('message_count'))
        .limit(10)
        .all()
    )

    top_senders = (
        db.query(
            func.coalesce(TelegramUser.username, TelegramUser.first_name, TelegramUser.last_name, TelegramUser.telegram_id).label('sender'),
            func.count(Message.id).label('message_count'),
        )
        .join(Message, Message.sender_user_id == TelegramUser.id)
        .group_by(TelegramUser.id, TelegramUser.username, TelegramUser.first_name, TelegramUser.last_name, TelegramUser.telegram_id)
        .order_by(desc('message_count'))
        .limit(10)
        .all()
    )

    top_keywords = (
        db.query(MessageKeyword.keyword, func.sum(MessageKeyword.weight).label('weight'))
        .group_by(MessageKeyword.keyword)
        .order_by(desc('weight'))
        .limit(settings.analysis_top_keywords)
        .all()
    )

    total_summaries = db.query(func.count(AiSummary.id)).scalar() or 0
    success_summaries = db.query(func.count(AiSummary.id)).filter(AiSummary.status == 'success').scalar() or 0
    total_urls = db.query(func.count(AiUrl.id)).scalar() or 0
    total_products = db.query(func.count(AiProduct.id)).scalar() or 0
    total_contacts = db.query(func.count(AiContact.id)).scalar() or 0
    total_alert_rules = db.query(func.count(AlertRule.id)).scalar() or 0
    unread_alerts = db.query(func.count(AlertMatch.id)).filter(AlertMatch.is_read.is_(False)).scalar() or 0

    return {
        'total_chats': total_chats,
        'active_chats': active_chats,
        'total_messages': total_messages,
        'messages_24h': messages_24h,
        'messages_7d': messages_7d,
        'total_users': total_users,
        'media_count': media_count,
        'total_summaries': total_summaries,
        'success_summaries': success_summaries,
        'total_urls': total_urls,
        'total_products': total_products,
        'total_contacts': total_contacts,
        'total_alert_rules': total_alert_rules,
        'unread_alerts': unread_alerts,
        'daily_rows': [{'day': str(day), 'count': count} for day, count in daily_rows],
        'hourly_dist': [{'hour': int(h), 'count': int(c)} for h, c in hourly_dist],
        'top_chats': [{'title': title, 'chat_id': cid, 'message_count': count} for title, cid, count in top_chats],
        'top_senders': [{'sender': str(sender), 'message_count': count} for sender, count in top_senders],
        'top_keywords': [{'keyword': keyword, 'weight': float(weight) if weight else 0} for keyword, weight in top_keywords],
    }


def chat_statistics(db: Session, chat_id: int) -> dict:
    now = datetime.utcnow()
    last_24h = now - timedelta(hours=24)

    total_messages = db.query(func.count(Message.id)).filter(Message.chat_id == chat_id).scalar() or 0
    messages_24h = db.query(func.count(Message.id)).filter(
        Message.chat_id == chat_id, Message.message_date >= last_24h
    ).scalar() or 0
    media_count = db.query(func.count(Message.id)).filter(
        Message.chat_id == chat_id, Message.has_media.is_(True)
    ).scalar() or 0
    unique_senders = db.query(func.count(func.distinct(Message.sender_user_id))).filter(
        Message.chat_id == chat_id, Message.sender_user_id.isnot(None)
    ).scalar() or 0

    first_msg = db.query(func.min(Message.message_date)).filter(Message.chat_id == chat_id).scalar()
    last_msg = db.query(func.max(Message.message_date)).filter(Message.chat_id == chat_id).scalar()

    top_senders = (
        db.query(
            func.coalesce(TelegramUser.username, TelegramUser.first_name, TelegramUser.telegram_id).label('sender'),
            func.count(Message.id).label('count'),
        )
        .join(Message, Message.sender_user_id == TelegramUser.id)
        .filter(Message.chat_id == chat_id)
        .group_by(TelegramUser.id)
        .order_by(desc('count'))
        .limit(5)
        .all()
    )

    summaries = db.query(func.count(AiSummary.id)).filter(AiSummary.chat_id == chat_id).scalar() or 0
    urls_found = db.query(func.count(func.distinct(AiUrlAppearance.url_id))).filter(
        AiUrlAppearance.chat_id == chat_id
    ).scalar() or 0

    daily_rows = (
        db.query(func.date(Message.message_date), func.count(Message.id))
        .filter(Message.chat_id == chat_id)
        .group_by(func.date(Message.message_date))
        .order_by(func.date(Message.message_date).desc())
        .limit(14)
        .all()
    )
    daily_rows.reverse()

    return {
        'total_messages': total_messages,
        'messages_24h': messages_24h,
        'media_count': media_count,
        'unique_senders': unique_senders,
        'first_message_at': first_msg,
        'last_message_at': last_msg,
        'top_senders': [{'sender': str(s), 'count': int(c)} for s, c in top_senders],
        'total_summaries': summaries,
        'daily_rows': [{'day': str(d), 'count': c} for d, c in daily_rows],
    }


def system_status(db: Session) -> dict:
    now = datetime.utcnow()
    last_24h = now - timedelta(hours=24)

    total_chats = db.query(func.count(MonitoredChat.id)).scalar() or 0
    active_chats = db.query(func.count(MonitoredChat.id)).filter(MonitoredChat.is_active.is_(True)).scalar() or 0
    total_messages = db.query(func.count(Message.id)).scalar() or 0
    messages_24h = db.query(func.count(Message.id)).filter(Message.message_date >= last_24h).scalar() or 0
    total_users = db.query(func.count(TelegramUser.id)).scalar() or 0
    total_summaries = db.query(func.count(AiSummary.id)).scalar() or 0
    total_urls = db.query(func.count(AiUrl.id)).scalar() or 0

    running_syncs = db.query(func.count(SyncRun.id)).filter(SyncRun.status == 'running').scalar() or 0
    failed_syncs_24h = db.query(func.count(SyncRun.id)).filter(
        SyncRun.status == 'failed', SyncRun.started_at >= last_24h
    ).scalar() or 0
    success_syncs_24h = db.query(func.count(SyncRun.id)).filter(
        SyncRun.status == 'success', SyncRun.started_at >= last_24h
    ).scalar() or 0

    running_summaries = db.query(func.count(AiSummary.id)).filter(AiSummary.status == 'running').scalar() or 0
    failed_summaries = db.query(func.count(AiSummary.id)).filter(AiSummary.status == 'failed').scalar() or 0

    recent_errors = db.query(SyncRun).filter(
        SyncRun.status == 'failed'
    ).order_by(SyncRun.id.desc()).limit(5).all()

    last_success = db.query(SyncRun).filter(
        SyncRun.status == 'success'
    ).order_by(SyncRun.id.desc()).first()

    return {
        'total_chats': total_chats,
        'active_chats': active_chats,
        'total_messages': total_messages,
        'messages_24h': messages_24h,
        'total_users': total_users,
        'total_summaries': total_summaries,
        'total_urls': total_urls,
        'running_syncs': running_syncs,
        'failed_syncs_24h': failed_syncs_24h,
        'success_syncs_24h': success_syncs_24h,
        'running_summaries': running_summaries,
        'failed_summaries': failed_summaries,
        'recent_errors': [
            {'id': r.id, 'message': r.message, 'started_at': r.started_at}
            for r in recent_errors
        ],
        'last_success_at': last_success.finished_at if last_success else None,
    }


def domain_frequency_stats(db: Session, limit: int = 20) -> list[dict]:
    rows = (
        db.query(AiUrl.domain, func.count(AiUrl.id).label('count'))
        .filter(AiUrl.domain.isnot(None), AiUrl.domain != '')
        .group_by(AiUrl.domain)
        .order_by(desc('count'))
        .limit(limit)
        .all()
    )
    return [{'domain': d, 'count': c} for d, c in rows]


def url_trend_data(db: Session, days: int = 30) -> list[dict]:
    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = (
        db.query(func.date(AiUrl.first_seen_at), func.count(AiUrl.id))
        .filter(AiUrl.first_seen_at >= cutoff)
        .group_by(func.date(AiUrl.first_seen_at))
        .order_by(func.date(AiUrl.first_seen_at))
        .all()
    )
    return [{'day': str(d), 'count': c} for d, c in rows]


def cross_chat_urls(db: Session, limit: int = 50) -> list[dict]:
    rows = (
        db.query(AiUrl)
        .filter(AiUrl.appearance_count > 1)
        .order_by(desc(AiUrl.appearance_count))
        .limit(limit)
        .all()
    )
    results = []
    for url in rows:
        chat_ids = url.chat_ids_seen or []
        chat_count = len(chat_ids) if isinstance(chat_ids, list) else 0
        results.append({
            'id': url.id,
            'url': url.url,
            'domain': url.domain,
            'title': None,
            'category': url.category,
            'appearance_count': url.appearance_count,
            'chat_count': chat_count,
            'reputation_score': url.reputation_score,
            'first_seen_at': url.first_seen_at,
        })
    return results


def url_reputation_summary(db: Session) -> dict:
    total = db.query(func.count(AiUrl.id)).scalar() or 0
    high = db.query(func.count(AiUrl.id)).filter(AiUrl.reputation_score >= 0.7).scalar() or 0
    medium = db.query(func.count(AiUrl.id)).filter(
        AiUrl.reputation_score >= 0.3, AiUrl.reputation_score < 0.7
    ).scalar() or 0
    low = db.query(func.count(AiUrl.id)).filter(
        AiUrl.reputation_score.isnot(None), AiUrl.reputation_score < 0.3
    ).scalar() or 0
    unscored = db.query(func.count(AiUrl.id)).filter(AiUrl.reputation_score.is_(None)).scalar() or 0
    return {
        'total': total,
        'high': high,
        'medium': medium,
        'low': low,
        'unscored': unscored,
    }

"""Advanced analytics: user profiling, anomaly detection, cross-chat aggregation."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, date
from collections import Counter, defaultdict

from sqlalchemy import func, desc, select
from sqlalchemy.orm import Session

from .models import (
    Message, MonitoredChat, TelegramUser, MessageKeyword,
    DailyChatStat, UserDailyStat, SystemEvent, MarketIntelligenceItem,
    AiProduct, ProductPriceHistory, AiUrl, AiUrlAppearance, AiContact,
    AiSummary, MessageFingerprint,
)

logger = logging.getLogger(__name__)


# ---------------------- User Profiling ----------------------

def build_user_profile(db: Session, user_id: int) -> dict:
    """Build a comprehensive user profile from historical data."""
    user = db.get(TelegramUser, user_id)
    if not user:
        return {}

    total_messages = db.query(func.count(Message.id)).filter(
        Message.sender_user_id == user_id
    ).scalar() or 0

    active_chats = db.query(func.count(func.distinct(Message.chat_id))).filter(
        Message.sender_user_id == user_id
    ).scalar() or 0

    # Top active chats
    top_chats = db.query(
        MonitoredChat.title,
        MonitoredChat.id,
        func.count(Message.id).label('cnt'),
    ).join(Message, Message.chat_id == MonitoredChat.id).filter(
        Message.sender_user_id == user_id
    ).group_by(MonitoredChat.id, MonitoredChat.title).order_by(desc('cnt')).limit(5).all()

    # Top keywords used
    top_keywords = db.query(
        MessageKeyword.keyword,
        func.sum(MessageKeyword.weight).label('w'),
    ).join(Message, MessageKeyword.message_id == Message.id).filter(
        Message.sender_user_id == user_id
    ).group_by(MessageKeyword.keyword).order_by(desc('w')).limit(10).all()

    # Product mentions (as seller)
    product_count = db.query(func.count(AiProduct.id)).filter(
        AiProduct.chat_id.in_(
            select(Message.chat_id).where(Message.sender_user_id == user_id)
        )
    ).scalar() or 0

    # Recent 7 day activity
    last_7d = db.query(func.count(Message.id)).filter(
        Message.sender_user_id == user_id,
        Message.message_date >= datetime.utcnow() - timedelta(days=7),
    ).scalar() or 0

    # Calculate role tags
    tags = []
    if product_count >= 3:
        tags.append('seller')
    if total_messages >= 100:
        tags.append('active')
    if active_chats >= 5:
        tags.append('cross_group')
    if last_7d == 0 and total_messages > 0:
        tags.append('inactive')

    # Estimated reputation score
    reputation = 0.5
    if 'seller' in tags:
        reputation += 0.2
    if total_messages > 50:
        reputation += min(0.2, total_messages / 1000)
    reputation = min(0.95, reputation)

    return {
        'user_id': user_id,
        'telegram_id': user.telegram_id,
        'username': user.username,
        'display_name': ' '.join(p for p in (user.first_name, user.last_name) if p) or user.username,
        'total_messages': total_messages,
        'active_chats': active_chats,
        'last_7d_messages': last_7d,
        'top_chats': [{'chat_id': cid, 'title': title, 'count': c} for title, cid, c in top_chats],
        'top_keywords': [{'keyword': k, 'weight': float(w)} for k, w in top_keywords],
        'product_mentions': product_count,
        'tags': tags,
        'reputation_score': round(reputation, 2),
        'first_seen': user.created_at.isoformat() if user.created_at else None,
    }


def compute_user_daily_aggregates(db: Session, target_date: date | None = None) -> int:
    """Backfill daily user stats for a given date."""
    target = target_date or date.today()
    start = datetime.combine(target, datetime.min.time())
    end = start + timedelta(days=1)

    rows = db.query(
        Message.sender_user_id,
        func.count(Message.id).label('msg_count'),
        func.sum(func.length(Message.normalized_text)).label('word_len'),
        func.sum(func.ifnull(Message.has_media, 0)).label('media_count'),
    ).filter(
        Message.sender_user_id.isnot(None),
        Message.message_date >= start,
        Message.message_date < end,
    ).group_by(Message.sender_user_id).all()

    updated = 0
    for user_id, msg_count, word_len, media_count in rows:
        stat = db.query(UserDailyStat).filter(
            UserDailyStat.user_id == user_id,
            func.date(UserDailyStat.date) == target,
        ).first()
        if stat:
            stat.message_count = msg_count
            stat.word_count = int(word_len or 0) // 6  # rough word estimate
            stat.media_count = int(media_count or 0)
        else:
            db.add(UserDailyStat(
                user_id=user_id,
                date=start,
                message_count=msg_count,
                word_count=int(word_len or 0) // 6,
                media_count=int(media_count or 0),
            ))
        updated += 1
    db.commit()
    return updated


# ---------------------- Chat Anomaly Detection ----------------------

def detect_chat_anomalies(db: Session) -> list[dict]:
    """Detect message volume spikes and new keyword bursts."""
    events: list[dict] = []
    now = datetime.utcnow()
    today = now.date()
    yesterday = today - timedelta(days=1)

    chats = db.query(MonitoredChat).filter(MonitoredChat.is_active.is_(True)).all()
    for chat in chats:
        today_stat = db.query(DailyChatStat).filter(
            DailyChatStat.chat_id == chat.id,
            func.date(DailyChatStat.date) == today,
        ).first()
        if not today_stat or not today_stat.message_count:
            continue

        # Average of previous 7 days
        baseline = db.query(func.avg(DailyChatStat.message_count)).filter(
            DailyChatStat.chat_id == chat.id,
            func.date(DailyChatStat.date) >= today - timedelta(days=8),
            func.date(DailyChatStat.date) < today,
        ).scalar() or 0

        if baseline > 0 and today_stat.message_count >= baseline * 3 and today_stat.message_count >= 50:
            events.append({
                'event_type': 'message_spike',
                'severity': 'warning' if today_stat.message_count >= baseline * 5 else 'info',
                'chat_id': chat.id,
                'title': f'消息量激增: {chat.title}',
                'detail': f'今日 {today_stat.message_count} 条，七日平均 {int(baseline)} 条',
                'metric_value': today_stat.message_count / max(baseline, 1),
            })

        # Detect new high-frequency keywords today vs yesterday
        if today_stat.top_keywords_json:
            today_kw = {item['keyword']: item.get('count', 0) for item in today_stat.top_keywords_json}
            yesterday_stat = db.query(DailyChatStat).filter(
                DailyChatStat.chat_id == chat.id,
                func.date(DailyChatStat.date) == yesterday,
            ).first()
            yesterday_kw: dict[str, int] = {}
            if yesterday_stat and yesterday_stat.top_keywords_json:
                yesterday_kw = {item['keyword']: item.get('count', 0) for item in yesterday_stat.top_keywords_json}

            for kw, cnt in today_kw.items():
                if cnt >= 5 and kw not in yesterday_kw:
                    events.append({
                        'event_type': 'new_keyword_burst',
                        'severity': 'info',
                        'chat_id': chat.id,
                        'title': f'新热点词出现: {kw}',
                        'detail': f'在 {chat.title} 今日出现 {cnt} 次',
                        'metric_value': cnt,
                    })

    # Persist new events
    created = 0
    for ev in events:
        existing = db.query(SystemEvent).filter(
            SystemEvent.event_type == ev['event_type'],
            SystemEvent.chat_id == ev.get('chat_id'),
            SystemEvent.title == ev['title'],
            SystemEvent.created_at >= now - timedelta(hours=6),
        ).first()
        if not existing:
            db.add(SystemEvent(
                event_type=ev['event_type'],
                severity=ev['severity'],
                chat_id=ev.get('chat_id'),
                title=ev['title'],
                detail=ev.get('detail'),
                metric_value=ev.get('metric_value'),
            ))
            created += 1
    if created:
        db.commit()

    return events


def get_unread_system_events(db: Session, severity: str | None = None, limit: int = 50) -> list[dict]:
    query = db.query(SystemEvent).filter(SystemEvent.is_read.is_(False))
    if severity:
        query = query.filter(SystemEvent.severity == severity)
    rows = query.order_by(SystemEvent.created_at.desc()).limit(limit).all()
    return [{
        'id': r.id,
        'event_type': r.event_type,
        'severity': r.severity,
        'chat_id': r.chat_id,
        'title': r.title,
        'detail': r.detail,
        'metric_value': r.metric_value,
        'created_at': r.created_at.isoformat() if r.created_at else None,
        'is_read': r.is_read,
    } for r in rows]


# ---------------------- Cross-Chat Market Aggregation ----------------------

def aggregate_market_intelligence(db: Session, hours: int = 24) -> dict:
    """Aggregate market intelligence signals across all chats in last N hours."""
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    rows = db.query(MarketIntelligenceItem).filter(
        MarketIntelligenceItem.created_at >= cutoff,
    ).all()

    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row.item_type].append({
            'content': row.content,
            'chat_id': row.chat_id,
            'summary_id': row.summary_id,
            'confidence': row.confidence,
        })

    # Count risk level
    risk_counts = Counter()
    summaries = db.query(AiSummary).filter(
        AiSummary.status == 'success',
        AiSummary.completed_at >= cutoff,
    ).all()
    for s in summaries:
        intel = (s.extracted_urls or {}).get('market_intelligence', {})
        level = str(intel.get('risk_level') or 'low').lower()
        if level in ('low', 'medium', 'high'):
            risk_counts[level] += 1

    # Top topics by frequency
    topic_counter: Counter[str] = Counter()
    for item in grouped.get('hotspot', []):
        topic_counter[item['content']] += 1
    for item in grouped.get('gossip', []):
        topic_counter[item['content']] += 1

    return {
        'period_hours': hours,
        'total_signals': len(rows),
        'risk_distribution': dict(risk_counts),
        'top_topics': [{'topic': t, 'count': c} for t, c in topic_counter.most_common(15)],
        'signals': {k: v[:30] for k, v in grouped.items()},
    }


def aggregate_url_propagation(db: Session, hours: int = 24, limit: int = 20) -> list[dict]:
    """Find URLs that appeared in multiple chats recently."""
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    rows = db.query(
        AiUrlAppearance.url_id,
        func.count(func.distinct(AiUrlAppearance.chat_id)).label('chat_count'),
        func.count(AiUrlAppearance.id).label('appear_count'),
    ).filter(
        AiUrlAppearance.seen_at >= cutoff,
    ).group_by(AiUrlAppearance.url_id).having(
        func.count(func.distinct(AiUrlAppearance.chat_id)) >= 2
    ).order_by(desc('chat_count')).limit(limit).all()

    results = []
    for url_id, chat_count, appear_count in rows:
        url = db.get(AiUrl, url_id)
        if not url:
            continue
        results.append({
            'url_id': url_id,
            'url': url.url,
            'domain': url.domain,
            'category': url.category,
            'chat_count': chat_count,
            'appear_count': appear_count,
            'reputation_score': url.reputation_score,
        })
    return results


# ---------------------- Price Analytics ----------------------

def get_price_trends(db: Session, chat_id: int | None = None, days: int = 30) -> list[dict]:
    """Return price history grouped by product."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    query = db.query(AiProduct).filter(AiProduct.last_seen_at >= cutoff)
    if chat_id:
        query = query.filter(AiProduct.chat_id == chat_id)
    products = query.order_by(AiProduct.product_name).limit(200).all()

    results = []
    for product in products:
        history = db.query(ProductPriceHistory).filter(
            ProductPriceHistory.product_id == product.id,
            ProductPriceHistory.recorded_at >= cutoff,
        ).order_by(ProductPriceHistory.recorded_at.asc()).all()

        prices = [h.price_amount for h in history if h.price_amount is not None]
        if len(prices) >= 2:
            change = prices[-1] - prices[0]
            change_pct = (change / prices[0] * 100) if prices[0] else 0
        else:
            change = 0
            change_pct = 0

        results.append({
            'product_id': product.id,
            'product_name': product.product_name,
            'chat_id': product.chat_id,
            'current_price': product.price_amount,
            'currency': product.price_currency,
            'seller': product.seller_contact,
            'status': product.status,
            'history': [
                {'price': h.price_amount, 'recorded_at': h.recorded_at.isoformat() if h.recorded_at else None}
                for h in history
            ],
            'price_change': round(change, 2),
            'price_change_pct': round(change_pct, 2),
        })
    return results


def aggregate_seller_prices(db: Session, product_name: str | None = None, days: int = 30) -> list[dict]:
    """Compare prices for the same product across sellers/chats."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    query = db.query(AiProduct).filter(AiProduct.last_seen_at >= cutoff)
    if product_name:
        query = query.filter(AiProduct.product_name.like(f'%{product_name}%'))
    products = query.limit(200).all()

    grouped: dict[str, list[dict]] = defaultdict(list)
    for p in products:
        grouped[p.product_name].append({
            'chat_id': p.chat_id,
            'price': p.price_amount,
            'currency': p.price_currency,
            'seller': p.seller_contact,
            'status': p.status,
            'last_seen': p.last_seen_at.isoformat() if p.last_seen_at else None,
        })

    results = []
    for name, items in grouped.items():
        prices = [i['price'] for i in items if i['price'] is not None]
        if not prices:
            continue
        results.append({
            'product_name': name,
            'seller_count': len(items),
            'min_price': min(prices),
            'max_price': max(prices),
            'avg_price': round(sum(prices) / len(prices), 2),
            'sellers': items,
        })
    return sorted(results, key=lambda x: x['seller_count'], reverse=True)[:100]


# ---------------------- Daily Chat Stats Computation ----------------------

def compute_daily_chat_stats(db: Session, target_date: date | None = None) -> int:
    """Backfill daily chat stats for a given date."""
    target = target_date or date.today()
    start = datetime.combine(target, datetime.min.time())
    end = start + timedelta(days=1)

    chats = db.query(MonitoredChat).all()
    updated = 0
    for chat in chats:
        messages = db.query(Message).filter(
            Message.chat_id == chat.id,
            Message.message_date >= start,
            Message.message_date < end,
        ).all()
        if not messages:
            continue

        msg_count = len(messages)
        unique_senders = len({m.sender_user_id for m in messages if m.sender_user_id})
        media_count = sum(1 for m in messages if m.has_media)
        total_len = sum(len(m.normalized_text or m.raw_text or '') for m in messages)
        url_count = sum(len(extract_urls_from_text(m.normalized_text or m.raw_text or '')) for m in messages)

        # top keywords
        kw_counter: Counter[str] = Counter()
        for m in messages:
            for kw in db.query(MessageKeyword).filter(MessageKeyword.message_id == m.id).all():
                kw_counter[kw.keyword] += kw.weight
        top_keywords = [{'keyword': k, 'count': c} for k, c in kw_counter.most_common(20)]

        stat = db.query(DailyChatStat).filter(
            DailyChatStat.chat_id == chat.id,
            func.date(DailyChatStat.date) == target,
        ).first()
        if stat:
            stat.message_count = msg_count
            stat.unique_senders = unique_senders
            stat.media_count = media_count
            stat.url_count = url_count
            stat.avg_message_length = total_len / msg_count if msg_count else 0
            stat.top_keywords_json = top_keywords
        else:
            db.add(DailyChatStat(
                chat_id=chat.id,
                date=start,
                message_count=msg_count,
                unique_senders=unique_senders,
                media_count=media_count,
                url_count=url_count,
                avg_message_length=total_len / msg_count if msg_count else 0,
                top_keywords_json=top_keywords,
            ))
        updated += 1
    db.commit()
    return updated


# ---------------------- Cross-Chat Duplicate Messages ----------------------

def get_duplicate_message_groups(db: Session, limit: int = 50) -> list[dict]:
    """Find messages that are duplicates across chats."""
    rows = db.query(MessageFingerprint).filter(
        MessageFingerprint.canonical_message_id.isnot(None),
    ).order_by(desc(MessageFingerprint.id)).limit(limit).all()

    canonical_ids = {r.canonical_message_id for r in rows if r.canonical_message_id}
    canonical_messages = {m.id: m for m in db.query(Message).filter(Message.id.in_(canonical_ids)).all()}

    results = []
    for row in rows:
        canonical = canonical_messages.get(row.canonical_message_id)
        if not canonical:
            continue
        results.append({
            'message_id': row.message_id,
            'fingerprint_hash': row.fingerprint_hash,
            'canonical_message_id': row.canonical_message_id,
            'canonical_chat_id': canonical.chat_id,
            'canonical_text': (canonical.raw_text or '')[:200],
        })
    return results


# Local helper to avoid circular imports
_URL_PATTERN = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")
_URL_TRAILING_PUNCTUATION = '.,;，。；）)]}>'


def _clean_url(raw_url: str) -> str:
    return raw_url.strip().rstrip(_URL_TRAILING_PUNCTUATION)


def extract_urls_from_text(text: str | None) -> list[str]:
    if not text:
        return []
    urls: list[str] = []
    seen: set[str] = set()
    for match in _URL_PATTERN.findall(text):
        url = _clean_url(match)
        if not url or not _URL_PATTERN.match(url):
            continue
        key = url.lower()
        if key in seen:
            continue
        seen.add(key)
        urls.append(url)
    return urls

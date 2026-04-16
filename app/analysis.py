from datetime import datetime, timedelta
from sqlalchemy import func, desc
from sqlalchemy.orm import Session

from .models import Message, MonitoredChat, TelegramUser, MessageKeyword
from .config import settings


def dashboard_metrics(db: Session) -> dict:
    now = datetime.utcnow()
    last_24h = now - timedelta(hours=24)

    total_chats = db.query(func.count(MonitoredChat.id)).scalar() or 0
    active_chats = db.query(func.count(MonitoredChat.id)).filter(MonitoredChat.is_active.is_(True)).scalar() or 0
    total_messages = db.query(func.count(Message.id)).scalar() or 0
    messages_24h = db.query(func.count(Message.id)).filter(Message.message_date >= last_24h).scalar() or 0
    total_users = db.query(func.count(TelegramUser.id)).scalar() or 0

    daily_rows = (
        db.query(func.date(Message.message_date), func.count(Message.id))
        .group_by(func.date(Message.message_date))
        .order_by(func.date(Message.message_date).desc())
        .limit(14)
        .all()
    )
    daily_rows.reverse()

    top_chats = (
        db.query(MonitoredChat.title, func.count(Message.id).label('message_count'))
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

    return {
        'total_chats': total_chats,
        'active_chats': active_chats,
        'total_messages': total_messages,
        'messages_24h': messages_24h,
        'total_users': total_users,
        'daily_rows': [{'day': str(day), 'count': count} for day, count in daily_rows],
        'top_chats': [{'title': title, 'message_count': count} for title, count in top_chats],
        'top_senders': [{'sender': str(sender), 'message_count': count} for sender, count in top_senders],
        'top_keywords': [{'keyword': keyword, 'weight': weight} for keyword, weight in top_keywords],
    }

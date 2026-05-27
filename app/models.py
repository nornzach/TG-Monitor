from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, BigInteger, String, Text, UniqueConstraint, JSON, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class AppSetting(Base):
    __tablename__ = 'app_settings'

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MonitoredChat(Base):
    __tablename__ = 'monitored_chats'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    access_hash: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    title: Mapped[str] = mapped_column(String(255), index=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    chat_type: Mapped[str] = mapped_column(String(50), default='unknown')
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_synced_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    messages: Mapped[list['Message']] = relationship('Message', back_populates='chat')
    ai_summaries: Mapped[list['AiSummary']] = relationship('AiSummary', back_populates='chat', cascade='all, delete-orphan')


class TelegramUser(Base):
    __tablename__ = 'telegram_users'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    about: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    messages: Mapped[list['Message']] = relationship('Message', back_populates='sender')


class Message(Base):
    __tablename__ = 'messages'
    __table_args__ = (
        UniqueConstraint('chat_id', 'telegram_message_id', name='uq_chat_message'),
        Index('idx_messages_message_date', 'message_date'),
        Index('idx_messages_chat_id_id', 'chat_id', 'id'),
        Index('idx_messages_chat_tg_msg', 'chat_id', 'telegram_message_id'),
        Index('idx_messages_chat_date', 'chat_id', 'message_date'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey('monitored_chats.id'), index=True)
    sender_user_id: Mapped[int | None] = mapped_column(ForeignKey('telegram_users.id'), nullable=True, index=True)
    telegram_message_id: Mapped[int] = mapped_column(Integer)
    message_date: Mapped[datetime] = mapped_column(DateTime, index=True)
    edit_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    reply_to_msg_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    views: Mapped[int | None] = mapped_column(Integer, nullable=True)
    forwards: Mapped[int | None] = mapped_column(Integer, nullable=True)
    has_media: Mapped[bool] = mapped_column(Boolean, default=False)
    media_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    meta_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    chat: Mapped[MonitoredChat] = relationship('MonitoredChat', back_populates='messages')
    sender: Mapped[TelegramUser | None] = relationship('TelegramUser', back_populates='messages')
    keywords: Mapped[list['MessageKeyword']] = relationship('MessageKeyword', back_populates='message', cascade='all, delete-orphan')


class MessageKeyword(Base):
    __tablename__ = 'message_keywords'
    __table_args__ = (Index('idx_keyword_keyword', 'keyword'),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(ForeignKey('messages.id'), index=True)
    keyword: Mapped[str] = mapped_column(String(100))
    weight: Mapped[int] = mapped_column(Integer, default=1)

    message: Mapped[Message] = relationship('Message', back_populates='keywords')


class SyncRun(Base):
    __tablename__ = 'sync_runs'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int | None] = mapped_column(ForeignKey('monitored_chats.id'), nullable=True)
    run_type: Mapped[str] = mapped_column(String(50), index=True)
    status: Mapped[str] = mapped_column(String(30), index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AiSummary(Base):
    __tablename__ = 'ai_summaries'
    __table_args__ = (
        Index('idx_summary_chat_status', 'chat_id', 'status'),
        Index('idx_summary_chat_status_end', 'chat_id', 'status', 'end_message_id'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey('monitored_chats.id'), index=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    start_message_id: Mapped[int] = mapped_column(Integer, default=0)
    end_message_id: Mapped[int] = mapped_column(Integer, default=0)
    summary_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_urls: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default='pending', index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    triggered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    chat: Mapped['MonitoredChat'] = relationship('MonitoredChat', back_populates='ai_summaries')


class AiUrl(Base):
    __tablename__ = 'ai_urls'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    url_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    category: Mapped[str] = mapped_column(String(20), index=True)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    appearance_count: Mapped[int] = mapped_column(Integer, default=1)
    chat_ids_seen: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    reputation_score: Mapped[float | None] = mapped_column(nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AiUrlAppearance(Base):
    __tablename__ = 'ai_url_appearances'
    __table_args__ = (
        Index('idx_url_appearance_chat', 'url_id', 'chat_id'),
        Index('idx_url_appearance_date', 'seen_at'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url_id: Mapped[int] = mapped_column(ForeignKey('ai_urls.id'), index=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey('monitored_chats.id'), index=True)
    summary_id: Mapped[int | None] = mapped_column(ForeignKey('ai_summaries.id'), nullable=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AiProduct(Base):
    __tablename__ = 'ai_products'
    __table_args__ = (
        Index('idx_product_chat_name', 'chat_id', 'product_name'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey('monitored_chats.id'), index=True)
    summary_id: Mapped[int | None] = mapped_column(ForeignKey('ai_summaries.id'), nullable=True, index=True)
    product_name: Mapped[str] = mapped_column(String(255), index=True)
    price_amount: Mapped[float | None] = mapped_column(nullable=True)
    price_currency: Mapped[str] = mapped_column(String(20), default='CNY')
    seller_contact: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default='available', index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    chat: Mapped['MonitoredChat'] = relationship('MonitoredChat', foreign_keys=[chat_id])
    summary: Mapped['AiSummary | None'] = relationship('AiSummary', foreign_keys=[summary_id])


class AiContact(Base):
    __tablename__ = 'ai_contacts'
    __table_args__ = (
        Index('idx_contact_chat_type_value', 'chat_id', 'contact_type', 'contact_value'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey('monitored_chats.id'), index=True)
    summary_id: Mapped[int | None] = mapped_column(ForeignKey('ai_summaries.id'), nullable=True, index=True)
    contact_type: Mapped[str] = mapped_column(String(30), index=True)
    contact_value: Mapped[str] = mapped_column(String(255), index=True)
    context: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    chat: Mapped['MonitoredChat'] = relationship('MonitoredChat', foreign_keys=[chat_id])
    summary: Mapped['AiSummary | None'] = relationship('AiSummary', foreign_keys=[summary_id])


class AlertRule(Base):
    __tablename__ = 'alert_rules'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100))
    pattern: Mapped[str] = mapped_column(Text)
    pattern_type: Mapped[str] = mapped_column(String(20), default='keyword')
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_web: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_telegram: Mapped[bool] = mapped_column(Boolean, default=False)
    chat_ids_filter: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AlertMatch(Base):
    __tablename__ = 'alert_matches'
    __table_args__ = (
        Index('idx_alert_match_rule_date', 'rule_id', 'matched_at'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey('alert_rules.id'), index=True)
    message_id: Mapped[int] = mapped_column(ForeignKey('messages.id'), index=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey('monitored_chats.id'), index=True)
    matched_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    matched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)

    rule: Mapped['AlertRule'] = relationship('AlertRule', foreign_keys=[rule_id])
    message: Mapped['Message'] = relationship('Message', foreign_keys=[message_id])
    chat: Mapped['MonitoredChat'] = relationship('MonitoredChat', foreign_keys=[chat_id])

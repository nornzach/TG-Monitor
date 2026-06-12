from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, BigInteger, String, Text, UniqueConstraint, JSON, Index
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


class TelegramJoinTarget(Base):
    __tablename__ = 'telegram_join_targets'
    __table_args__ = (
        UniqueConstraint('normalized_key', name='uq_join_target_key'),
        Index('idx_join_target_status_next', 'status', 'next_attempt_at'),
        Index('idx_join_target_monitored_chat', 'monitored_chat_id'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(500), nullable=False)
    normalized_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(30), default='unknown', index=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default='pending', index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    joined_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    monitored_chat_id: Mapped[int | None] = mapped_column(ForeignKey('monitored_chats.id'), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    monitored_chat: Mapped['MonitoredChat | None'] = relationship('MonitoredChat', foreign_keys=[monitored_chat_id])


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
    classification_status: Mapped[str] = mapped_column(String(20), default='pending', index=True)
    primary_category_id: Mapped[int | None] = mapped_column(ForeignKey('ai_url_categories.id'), nullable=True, index=True)
    classification_run_id: Mapped[int | None] = mapped_column(ForeignKey('ai_url_classification_runs.id'), nullable=True, index=True)
    classified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    classification_error: Mapped[str | None] = mapped_column(Text, nullable=True)
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


class AiUrlCategory(Base):
    __tablename__ = 'ai_url_categories'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100), index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(20), default='ai', index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AiUrlClassificationRun(Base):
    __tablename__ = 'ai_url_classification_runs'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    status: Mapped[str] = mapped_column(String(30), default='running', index=True)
    batch_size: Mapped[int] = mapped_column(Integer, default=50)
    total_urls: Mapped[int] = mapped_column(Integer, default=0)
    processed_urls: Mapped[int] = mapped_column(Integer, default=0)
    created_categories: Mapped[int] = mapped_column(Integer, default=0)
    prompt_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AiUrlClassification(Base):
    __tablename__ = 'ai_url_classifications'
    __table_args__ = (
        UniqueConstraint('url_id', 'category_id', name='uq_url_category'),
        Index('idx_url_classification_run', 'run_id'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url_id: Mapped[int] = mapped_column(ForeignKey('ai_urls.id'), index=True)
    category_id: Mapped[int] = mapped_column(ForeignKey('ai_url_categories.id'), index=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey('ai_url_classification_runs.id'), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


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


class AiKeyLeadRun(Base):
    __tablename__ = 'ai_key_lead_runs'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    status: Mapped[str] = mapped_column(String(30), default='running', index=True)
    batch_size: Mapped[int] = mapped_column(Integer, default=200)
    total_messages: Mapped[int] = mapped_column(Integer, default=0)
    processed_leads: Mapped[int] = mapped_column(Integer, default=0)
    start_message_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    end_message_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    prompt_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AiKeyLead(Base):
    __tablename__ = 'ai_key_leads'
    __table_args__ = (
        UniqueConstraint('content_hash', name='uq_key_lead_content_hash'),
        Index('idx_key_lead_provider_type', 'provider', 'lead_type'),
        Index('idx_key_lead_chat_seen', 'chat_id', 'last_seen_at'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey('ai_key_lead_runs.id'), nullable=True, index=True)
    message_id: Mapped[int] = mapped_column(ForeignKey('messages.id'), index=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey('monitored_chats.id'), index=True)
    sender_user_id: Mapped[int | None] = mapped_column(ForeignKey('telegram_users.id'), nullable=True, index=True)
    lead_type: Mapped[str] = mapped_column(String(30), index=True)
    provider: Mapped[str | None] = mapped_column(String(60), nullable=True, index=True)
    product_name: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    offer_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    price_amount: Mapped[float | None] = mapped_column(nullable=True)
    price_currency: Mapped[str | None] = mapped_column(String(20), nullable=True)
    seller_contact: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    seller_telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    seller_username: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    seller_display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    run: Mapped['AiKeyLeadRun | None'] = relationship('AiKeyLeadRun', foreign_keys=[run_id])
    message: Mapped['Message'] = relationship('Message', foreign_keys=[message_id])
    chat: Mapped['MonitoredChat'] = relationship('MonitoredChat', foreign_keys=[chat_id])
    sender: Mapped['TelegramUser | None'] = relationship('TelegramUser', foreign_keys=[sender_user_id])


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


class MessageEdit(Base):
    __tablename__ = 'message_edits'
    __table_args__ = (
        Index('idx_message_edits_message_id', 'message_id'),
        Index('idx_message_edits_edit_date', 'edit_date'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(ForeignKey('messages.id', ondelete='CASCADE'), index=True)
    old_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    edit_date: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MessageReaction(Base):
    __tablename__ = 'message_reactions'
    __table_args__ = (
        UniqueConstraint('message_id', 'reaction_type', name='uq_message_reaction'),
        Index('idx_message_reactions_message_id', 'message_id'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(ForeignKey('messages.id', ondelete='CASCADE'), index=True)
    reaction_type: Mapped[str] = mapped_column(String(100), default='like')
    count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MessageFingerprint(Base):
    __tablename__ = 'message_fingerprints'
    __table_args__ = (
        UniqueConstraint('fingerprint_hash', name='uq_fingerprint_hash'),
        Index('idx_message_fprints_msg_id', 'message_id'),
        Index('idx_message_fprints_similarity', 'similarity_hash'),
        Index('idx_message_fprints_canonical', 'canonical_message_id'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(ForeignKey('messages.id', ondelete='CASCADE'), index=True)
    fingerprint_hash: Mapped[str] = mapped_column(String(64))
    similarity_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    canonical_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MessageViewsHistory(Base):
    __tablename__ = 'message_views_history'
    __table_args__ = (
        Index('idx_views_history_message_id', 'message_id'),
        Index('idx_views_history_recorded', 'recorded_at'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(ForeignKey('messages.id', ondelete='CASCADE'), index=True)
    views: Mapped[int] = mapped_column(Integer, default=0)
    forwards: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class UserDailyStat(Base):
    __tablename__ = 'user_daily_stats'
    __table_args__ = (
        UniqueConstraint('user_id', 'date', name='uq_user_daily_stats'),
        Index('idx_user_daily_stats_user_id', 'user_id'),
        Index('idx_user_daily_stats_date', 'date'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('telegram_users.id', ondelete='CASCADE'), index=True)
    date: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    media_count: Mapped[int] = mapped_column(Integer, default=0)
    active_hours_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    top_chats_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    reputation_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ProductPriceHistory(Base):
    __tablename__ = 'product_price_history'
    __table_args__ = (
        Index('idx_price_history_product_id', 'product_id'),
        Index('idx_price_history_recorded', 'recorded_at'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(ForeignKey('ai_products.id', ondelete='CASCADE'), index=True)
    price_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_currency: Mapped[str] = mapped_column(String(20), default='CNY')
    source_message_id: Mapped[int | None] = mapped_column(ForeignKey('messages.id', ondelete='SET NULL'), nullable=True)
    seller_contact: Mapped[str | None] = mapped_column(String(255), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    product: Mapped['AiProduct'] = relationship('AiProduct', foreign_keys=[product_id])


class MarketIntelligenceItem(Base):
    __tablename__ = 'market_intelligence_items'
    __table_args__ = (
        Index('idx_market_intel_summary_id', 'summary_id'),
        Index('idx_market_intel_chat_id', 'chat_id'),
        Index('idx_market_intel_item_type', 'item_type'),
        Index('idx_market_intel_created', 'created_at'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    summary_id: Mapped[int] = mapped_column(ForeignKey('ai_summaries.id', ondelete='CASCADE'), index=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey('monitored_chats.id', ondelete='CASCADE'), index=True)
    item_type: Mapped[str] = mapped_column(String(30), index=True)
    content: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    related_entities_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class UrlMetadata(Base):
    __tablename__ = 'url_metadata'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url_id: Mapped[int] = mapped_column(ForeignKey('ai_urls.id', ondelete='CASCADE'), unique=True)
    page_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    page_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    screenshot_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SummaryUrl(Base):
    __tablename__ = 'summary_urls'
    __table_args__ = (
        UniqueConstraint('summary_id', 'url_id', name='uq_summary_url'),
        Index('idx_summary_urls_summary_id', 'summary_id'),
        Index('idx_summary_urls_url_id', 'url_id'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    summary_id: Mapped[int] = mapped_column(ForeignKey('ai_summaries.id', ondelete='CASCADE'), index=True)
    url_id: Mapped[int] = mapped_column(ForeignKey('ai_urls.id', ondelete='CASCADE'), index=True)
    url_type: Mapped[str] = mapped_column(String(20), default='other')


class DailyChatStat(Base):
    __tablename__ = 'daily_chat_stats'
    __table_args__ = (
        UniqueConstraint('chat_id', 'date', name='uq_daily_chat_stats'),
        Index('idx_daily_chat_stats_chat_id', 'chat_id'),
        Index('idx_daily_chat_stats_date', 'date'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey('monitored_chats.id', ondelete='CASCADE'), index=True)
    date: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    unique_senders: Mapped[int] = mapped_column(Integer, default=0)
    media_count: Mapped[int] = mapped_column(Integer, default=0)
    url_count: Mapped[int] = mapped_column(Integer, default=0)
    new_user_count: Mapped[int] = mapped_column(Integer, default=0)
    top_keywords_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    avg_message_length: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SystemEvent(Base):
    __tablename__ = 'system_events'
    __table_args__ = (
        Index('idx_system_events_type', 'event_type'),
        Index('idx_system_events_severity', 'severity'),
        Index('idx_system_events_chat_id', 'chat_id'),
        Index('idx_system_events_created', 'created_at'),
        Index('idx_system_events_is_read', 'is_read'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(50), index=True)
    severity: Mapped[str] = mapped_column(String(20), default='info')
    chat_id: Mapped[int | None] = mapped_column(ForeignKey('monitored_chats.id', ondelete='CASCADE'), nullable=True, index=True)
    message_id: Mapped[int | None] = mapped_column(ForeignKey('messages.id', ondelete='SET NULL'), nullable=True)
    title: Mapped[str] = mapped_column(String(255))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    metric_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DailyMarketBrief(Base):
    __tablename__ = 'daily_market_briefs'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    brief_date: Mapped[datetime] = mapped_column(DateTime, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text)
    signals_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    hot_topics_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    risk_level: Mapped[str] = mapped_column(String(20), default='low')
    price_moves_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    generated_by: Mapped[str] = mapped_column(String(50), default='ai')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

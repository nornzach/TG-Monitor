from contextlib import contextmanager
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base

from .config import settings

engine = create_engine(settings.sqlalchemy_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)
Base = declarative_base()


def init_database() -> None:
    admin_engine = create_engine(settings.admin_sqlalchemy_url, future=True)
    with admin_engine.begin() as conn:
        conn.execute(text(
            f"CREATE DATABASE IF NOT EXISTS `{settings.database_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        ))
    admin_engine.dispose()
    from .models import MonitoredChat, TelegramUser, Message, MessageKeyword, SyncRun, AppSetting, AiSummary, AiUrl, AiUrlAppearance, AiProduct, AiContact, AlertRule, AlertMatch  # noqa
    Base.metadata.create_all(bind=engine)
    ensure_runtime_indexes()


def ensure_runtime_indexes() -> None:
    inspector = inspect(engine)
    if not inspector.has_table('messages') or not inspector.has_table('ai_summaries'):
        return

    message_indexes = {idx['name'] for idx in inspector.get_indexes('messages')}
    summary_indexes = {idx['name'] for idx in inspector.get_indexes('ai_summaries')}

    statements = []
    if 'idx_messages_chat_id_id' not in message_indexes:
        statements.append('CREATE INDEX idx_messages_chat_id_id ON messages (chat_id, id)')
    if 'idx_messages_chat_tg_msg' not in message_indexes:
        statements.append('CREATE INDEX idx_messages_chat_tg_msg ON messages (chat_id, telegram_message_id)')
    if 'idx_messages_chat_date' not in message_indexes:
        statements.append('CREATE INDEX idx_messages_chat_date ON messages (chat_id, message_date)')
    if 'idx_summary_chat_status_end' not in summary_indexes:
        statements.append('CREATE INDEX idx_summary_chat_status_end ON ai_summaries (chat_id, status, end_message_id)')

    # Migrate TelegramUser table - add about column if missing
    if inspector.has_table('telegram_users'):
        user_columns = {col['name'] for col in inspector.get_columns('telegram_users')}
        if 'about' not in user_columns:
            statements.append('ALTER TABLE telegram_users ADD COLUMN about TEXT NULL')

    # Migrate AiUrl table - add new columns if they don't exist
    if inspector.has_table('ai_urls'):
        url_columns = {col['name'] for col in inspector.get_columns('ai_urls')}
        if 'domain' not in url_columns:
            statements.append('ALTER TABLE ai_urls ADD COLUMN domain VARCHAR(255) NULL')
            statements.append('CREATE INDEX idx_ai_urls_domain ON ai_urls (domain)')
        if 'appearance_count' not in url_columns:
            statements.append('ALTER TABLE ai_urls ADD COLUMN appearance_count INT DEFAULT 1')
        if 'chat_ids_seen' not in url_columns:
            statements.append('ALTER TABLE ai_urls ADD COLUMN chat_ids_seen JSON NULL')
        if 'reputation_score' not in url_columns:
            statements.append('ALTER TABLE ai_urls ADD COLUMN reputation_score FLOAT NULL')

    if not statements:
        return

    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


@contextmanager
def session_scope():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

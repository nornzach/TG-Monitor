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
    from .models import (
        MonitoredChat, TelegramJoinTarget, TelegramUser, Message, MessageKeyword,
        SyncRun, AppSetting, AiSummary, AiUrl, AiUrlAppearance, AiUrlCategory,
        AiUrlClassificationRun, AiUrlClassification, AiProduct, AiContact,
        AiKeyLeadRun, AiKeyLead, AlertRule, AlertMatch,
        MessageEdit, MessageReaction, MessageFingerprint, MessageViewsHistory,
        UserDailyStat, ProductPriceHistory, MarketIntelligenceItem, UrlMetadata,
        SummaryUrl, DailyChatStat, SystemEvent, DailyMarketBrief,
    )  # noqa
    Base.metadata.create_all(bind=engine)
    ensure_runtime_indexes()
    seed_default_url_categories()


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
        url_indexes = {idx['name'] for idx in inspector.get_indexes('ai_urls')}
        if 'domain' not in url_columns:
            statements.append('ALTER TABLE ai_urls ADD COLUMN domain VARCHAR(255) NULL')
            statements.append('CREATE INDEX idx_ai_urls_domain ON ai_urls (domain)')
        if 'appearance_count' not in url_columns:
            statements.append('ALTER TABLE ai_urls ADD COLUMN appearance_count INT DEFAULT 1')
        if 'chat_ids_seen' not in url_columns:
            statements.append('ALTER TABLE ai_urls ADD COLUMN chat_ids_seen JSON NULL')
        if 'reputation_score' not in url_columns:
            statements.append('ALTER TABLE ai_urls ADD COLUMN reputation_score FLOAT NULL')
        if 'classification_status' not in url_columns:
            statements.append("ALTER TABLE ai_urls ADD COLUMN classification_status VARCHAR(20) NOT NULL DEFAULT 'pending'")
            statements.append('CREATE INDEX ix_ai_urls_classification_status ON ai_urls (classification_status)')
        elif 'ix_ai_urls_classification_status' not in url_indexes:
            statements.append('CREATE INDEX ix_ai_urls_classification_status ON ai_urls (classification_status)')
        if 'primary_category_id' not in url_columns:
            statements.append('ALTER TABLE ai_urls ADD COLUMN primary_category_id INT NULL')
            statements.append('CREATE INDEX ix_ai_urls_primary_category_id ON ai_urls (primary_category_id)')
        elif 'ix_ai_urls_primary_category_id' not in url_indexes:
            statements.append('CREATE INDEX ix_ai_urls_primary_category_id ON ai_urls (primary_category_id)')
        if 'classification_run_id' not in url_columns:
            statements.append('ALTER TABLE ai_urls ADD COLUMN classification_run_id INT NULL')
            statements.append('CREATE INDEX ix_ai_urls_classification_run_id ON ai_urls (classification_run_id)')
        elif 'ix_ai_urls_classification_run_id' not in url_indexes:
            statements.append('CREATE INDEX ix_ai_urls_classification_run_id ON ai_urls (classification_run_id)')
        if 'classified_at' not in url_columns:
            statements.append('ALTER TABLE ai_urls ADD COLUMN classified_at DATETIME NULL')
        if 'classification_error' not in url_columns:
            statements.append('ALTER TABLE ai_urls ADD COLUMN classification_error TEXT NULL')

    if inspector.has_table('ai_key_leads'):
        key_lead_columns = {col['name'] for col in inspector.get_columns('ai_key_leads')}
        key_lead_indexes = {idx['name'] for idx in inspector.get_indexes('ai_key_leads')}
        if 'seller_telegram_id' not in key_lead_columns:
            statements.append('ALTER TABLE ai_key_leads ADD COLUMN seller_telegram_id BIGINT NULL')
            statements.append('CREATE INDEX ix_ai_key_leads_seller_telegram_id ON ai_key_leads (seller_telegram_id)')
        elif 'ix_ai_key_leads_seller_telegram_id' not in key_lead_indexes:
            statements.append('CREATE INDEX ix_ai_key_leads_seller_telegram_id ON ai_key_leads (seller_telegram_id)')
        if 'seller_username' not in key_lead_columns:
            statements.append('ALTER TABLE ai_key_leads ADD COLUMN seller_username VARCHAR(255) NULL')
            statements.append('CREATE INDEX ix_ai_key_leads_seller_username ON ai_key_leads (seller_username)')
        elif 'ix_ai_key_leads_seller_username' not in key_lead_indexes:
            statements.append('CREATE INDEX ix_ai_key_leads_seller_username ON ai_key_leads (seller_username)')
        if 'seller_display_name' not in key_lead_columns:
            statements.append('ALTER TABLE ai_key_leads ADD COLUMN seller_display_name VARCHAR(255) NULL')

    if not statements:
        return

    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


def seed_default_url_categories() -> None:
    from .models import AiUrlCategory

    defaults = [
        ('telegram_group', 'Telegram 群组/频道', 't.me 群组、频道、加群邀请链接'),
        ('cloud_drive', '网盘/文件分享', '夸克网盘、百度网盘、阿里云盘等文件分享链接'),
        ('code_repository', '代码仓库/项目', 'GitHub、GitLab、Gitee 等代码项目地址'),
        ('relay_service', '中转/节点服务', '节点销售、VPS、代理、VPN、流量转发服务'),
        ('account_seller', '账号/号码交易', 'Telegram 账号、号码、接码、实名账号交易'),
        ('payment_store', '支付/店铺链接', '支付页面、店铺、商品购买或充值链接'),
        ('ai_tool', 'AI 工具/模型服务', 'AI 工具、模型 API、提示词或自动化服务'),
        ('documentation', '文档/教程', '文档、教程、博客文章、说明页面'),
        ('social_profile', '社交主页', '个人主页、社交媒体主页或联系方式页'),
        ('generic_link', '普通链接', '不能归入更细类别的普通网页'),
        ('other', '其他', '无法判断或暂不适合细分的链接'),
    ]
    with SessionLocal() as session:
        existing = {row.slug for row in session.query(AiUrlCategory).all()}
        for slug, name, description in defaults:
            if slug in existing:
                continue
            session.add(AiUrlCategory(
                slug=slug,
                name=name,
                description=description,
                source='seed',
                is_active=True,
            ))
        session.commit()


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

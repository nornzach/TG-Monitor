#!/usr/bin/env python3
"""Migration script to add advanced analytics tables.

Run after upgrading the application:
    python scripts/migrate_advanced_analytics.py

This will create all new tables introduced in the advanced analytics upgrade.
Existing data is preserved. New tables include:
    - message_edits
    - message_reactions
    - message_fingerprints
    - message_views_history
    - user_daily_stats
    - product_price_history
    - market_intelligence_items
    - url_metadata
    - summary_urls
    - daily_chat_stats
    - system_events
    - daily_market_briefs
"""
from __future__ import annotations

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import engine, init_database
from app.models import (
    MessageEdit, MessageReaction, MessageFingerprint, MessageViewsHistory,
    UserDailyStat, ProductPriceHistory, MarketIntelligenceItem, UrlMetadata,
    SummaryUrl, DailyChatStat, SystemEvent, DailyMarketBrief,
)


def main() -> None:
    print('Creating advanced analytics tables...')
    # Importing models registers them with Base.metadata
    tables = [
        MessageEdit.__table__, MessageReaction.__table__, MessageFingerprint.__table__,
        MessageViewsHistory.__table__, UserDailyStat.__table__, ProductPriceHistory.__table__,
        MarketIntelligenceItem.__table__, UrlMetadata.__table__, SummaryUrl.__table__,
        DailyChatStat.__table__, SystemEvent.__table__, DailyMarketBrief.__table__,
    ]
    for table in tables:
        table.create(engine, checkfirst=True)
        print(f'  - {table.name}: ok')
    init_database()  # Also ensure runtime indexes and seed categories
    print('Migration complete.')


if __name__ == '__main__':
    main()

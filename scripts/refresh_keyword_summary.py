"""Backfill / refresh the keyword_summary materialized table.

Run manually or schedule periodically:
    .venv/bin/python scripts/refresh_keyword_summary.py
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import text

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import SessionLocal, engine
from app.models import Base, KeywordSummary, Message, MessageKeyword

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def ensure_table():
    Base.metadata.create_all(engine, tables=[KeywordSummary.__table__])


def refresh_full():
    ensure_table()
    db = SessionLocal()
    try:
        logger.info('truncating keyword_summary')
        db.execute(text('TRUNCATE TABLE keyword_summary'))
        logger.info('aggregating message_keywords (this may take a while)...')
        db.execute(text('''
            INSERT INTO keyword_summary (keyword, total_weight, updated_at)
            SELECT keyword, SUM(weight), NOW()
            FROM message_keywords
            GROUP BY keyword
        '''))
        db.commit()
        count = db.query(KeywordSummary).count()
        logger.info('keyword_summary refreshed with %d rows', count)
    finally:
        db.close()


def refresh_recent(days: int = 30):
    """Incremental-ish refresh: re-aggregate keywords from recent messages.

    Since weights are sums and we don't track per-day deltas, we recompute
    the global total by adding recent-only weight on top of a stale base.
    For simplicity this script recomputes the full table; schedule it during
    low-traffic periods.
    """
    refresh_full()


if __name__ == '__main__':
    refresh_full()

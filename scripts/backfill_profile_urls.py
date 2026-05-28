from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import select

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.ai_service import extract_urls_from_text, upsert_discovered_urls
from app.db import SessionLocal
from app.models import Message, TelegramUser


def main() -> None:
    db = SessionLocal()
    try:
        users = db.execute(
            select(TelegramUser.id, TelegramUser.about).where(TelegramUser.about.is_not(None))
        ).all()

        users_with_profile_urls = 0
        profile_urls_found = 0
        upserted_urls = 0
        errors = 0

        for user_id, about in users:
            urls = extract_urls_from_text(about)
            if not urls:
                continue

            users_with_profile_urls += 1
            profile_urls_found += len(urls)
            chat_id = db.execute(
                select(Message.chat_id)
                .where(Message.sender_user_id == user_id)
                .order_by(Message.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            try:
                upserted_urls += upsert_discovered_urls(urls, category='other', chat_id=chat_id)
            except Exception:
                errors += 1

        print({
            'users_with_profile_urls': users_with_profile_urls,
            'profile_urls_found': profile_urls_found,
            'upserted_urls': upserted_urls,
            'errors': errors,
        })
    finally:
        db.close()


if __name__ == '__main__':
    main()
